# ROS Library
import rclpy
from rclpy.node import Node

# Other
from threading import Thread, Lock
import sys
import time
from pathlib import Path
import importlib.util
from random import random
import json

# ROS messages and services
from workcell_interfaces.srv import *
from workcell_interfaces.msg import *

# OT2_workcell_manager API
from ot2_workcell_manager_client.retry_api import *
from ot2_workcell_manager_client.register_api import *
from ot2_workcell_manager_client.register_api import _register, _deregister_node
from ot2_workcell_manager_client.worker_info_api import *
from ot2_workcell_manager_client.worker_info_api import (
    _get_node_info,
    get_node_list,
    get_node_info,
    node_ready,
    _node_ready,
)

# OT2 Control API
from ot2_client.ot2_control_api import load_protocols_to_ot2, add_work_to_ot2

# scheduler_client 
from scheduler_client.publish_scheduler_state import _update_scheduler_state, update_scheduler_state

'''
    The schedulerManager node is responsible for scheduling protocols across the OT2s this means it requires state information and needs to interact with the database. 

    TODO: 2 node system like arm and ot2 or 1 node system like master 
'''
class schedulerManager(Node):
    def __init__(self, name):
        ''' TODO: Maybe add this
        super().__init__("Temp" + str(int(random() * 17237534)))

        # Parameters before we register with master
        self.declare_parameter(
            "name", "insert_scheduler_name_here"
        )  # 2nd arg is default value
        time.sleep(2) # Wait for the launch file to hand in names
        name = self.get_parameter("name").get_parameter_value().string_value
        while name == "temp" or name == "insert_scheduler_name_here":
            self.get_logger().info("Please enter parameter node name")
            rclpy.spin_once(self) # spin self once for parameter
            name = self.get_parameter("name").get_parameter_value().string_value
        '''
        # Node creation
        super().__init__("scheduler_manager_" + name)  # User specifies name


        # Readabilty
        self.state = {  # TODO: maybe a sync with the master
            "BUSY": 1,
            "READY": 0,
            "ERROR": 2,
            "QUEUED": 3,
            "COMPLETED": 4
        }
        self.status = {"ERROR": 1, "SUCCESS": 0, "WARNING": 2, "FATAL": 3, "COMPLETED": 4,"WAITING": 10}

        # Protocol queue
        self.queue_lock = Lock()
        self.protocol_queue = [] # protocols to run 

        # Block to robot map 
        ''' Structure 
            Maps "Block-name":"Robot-name" 

            Common Case) 
            If the block has been assigned to a robot then the translation is easy, just replace all block names with the robot name 
        '''
        self.block_to_robot_map = {}

        # State information
        self.current_state = self.state["READY"]  # Start ready
        self.block_name_tag = 0                  # Increments for each new block, puts a unique tag in front of each
        self.dead = False # kill command

        # Path setup
        path = Path()
        self.home_location = str(path.home())
        self.module_location = self.home_location + "/ot2_ws/src/ot2_workcell/OT2_Modules/" #TODO: this might change 

        # Register with master
        args = []
        args.append(self)  # Self
        args.append("scheduler")  # Type
        args.append(name)  # Name
        status = retry(
            self, _register, 1000, 1, args
        )  # Setups up a retry system for a function
        if status == self.status["ERROR"] or status == self.status["FATAL"]:
            self.get_logger().fatal("Unable to register with master, exiting...")
            sys.exit(1)  # Can't register node even after retrying

        # Services and Subscribers
        self.get_id_service = self.create_service( # Service to add work to the queue 
            SchedulerWork, "/scheduler/add_work", self.add_work_handler 
        )
        self.get_id_service = self.create_service( # Service to get robot-name from block-name
            BlockToRobot, "/scheduler/block_to_robot", self.block_to_robot_handler
        )
        # This subscriber lets us know which block has finished 
        self.OT2_state_subscriber = self.create_subscription(OT2StateUpdate, "/OT_2/ot2_state_update", self.node_state_update_callback, 10) 
        self.OT2_state_subscriber 
        self.arm_state_reset_subscriber = self.create_subscription(PeelerStateUpdate, "/peeler/peeler_state_update", self.node_state_update_callback, 10)
        self.arm_state_reset_subscriber  
        self.arm_state_reset_subscriber = self.create_subscription(SealerStateUpdate, "/sealer/sealer_state_update", self.node_state_update_callback, 10)
        self.arm_state_reset_subscriber
        self.arm_state_reset_subscriber = self.create_subscription(PlateStackerStateUpdate, "/platestacker/platestacker_state_update", self.node_state_update_callback, 10)
        self.arm_state_reset_subscriber                  
        # Initialization Complete
        self.get_logger().info(
            "Scheduler Manager for ID: %s name: %s initialization completed"
            % (self.id, self.name)
        )

    '''
        This reads from a setup file in the OT2_Modules directory which contains the work for each robot that needs to be 
        run. Currently it is possible for the system to deadlock due to circular wait with the transfer requests, since 
        both robots need to be ready for the arm (Technically the OT2 are the resource as it waits on the other robot). 
        This could cause issues that need to be addressed in the future. 

        For testing purposes! 
    '''
    def read_from_setup(self, file):
        # Read from setup file and distrubute to worker threads - Read number of threads
        f = open(
            self.module_location + file, "r"
        )  # Open up file "setup" in well-known directory
        n = int(
            f.readline()
        )  # First line should contain an integer, corresponds to number of threads

        # Load each block 
        for i in range(n):  # Starts reading names and files to be run

            # Get block for the worker
            try:
                block = f.readline()
            except Exception as e:
                self.get_logger().error("Reading from setup error: %r" % (e,))
                return self.status["ERROR"]  # Error

            split_block = block.split()
            self.queue_lock.acquire() # enter critical section
            self.protocol_queue.append(split_block) # Add to batch queue for scheduling 
            self.queue_lock.release()

        self.get_logger().info("Setup file read and run complete")
        return self.status["SUCCESS"]

    '''
        This service handler adds the request work to the manager queue to be scheduled

        The request is a list of strings (each string is a block and will be split the same way the read_from_setup functions splits) 

        TODO: error handling
    '''
    def add_work_handler(self, request, response): 

        # Create response 
        response = SchedulerWork.Response()

        # Get blocks 
        datastr = request.jsonblocks
        data = json.loads(datastr)
        blocks = data['blocks'] 

        # Add tag to each block and determine unique names within a block
        name_set = set()
        for block in blocks:
            # Uniqueness check within workflow file
            try:
                if(block['block-name'] in name_set):
                    response.status = response.ERROR
                    self.get_logger().error("Duplicate block name %s, workflow file rejected"%(block['block-name']))
                    return response # Error
                else:
                    name_set.add(block['block-name'])
            except Exception as e:
                self.get_logger().error("Error %r"%(e,))
            
            # Attach tag
            block['block-name'] = str(self.block_name_tag)+"-"+block['block-name']
            block['tag'] = self.block_name_tag                  # for later down the line, the tag needs to be added to all transfers

        # Increment tags to maintain uniqueness 
        self.block_name_tag += 1

        # Parse and add each block 
        self.queue_lock.acquire() # Enter critical section 
        for block in blocks: 
            self.protocol_queue.append(block) # Add to batch queue for scheduling 
        self.queue_lock.release()

        # return status 
        response.status = response.SUCCESS
        return response
   
    '''
        Processes the block_to_robot service call.
        If status == success the mapping was a success
        if status == error then their is no mapping currently 
    '''
    def block_to_robot_handler(self, request, response): 

        # create Response 
        response = BlockToRobot.Response() 

        # Get block name
        block_name = request.block_name

        # Search datastructure for block name 
        if block_name in self.block_to_robot_map: # exists 
            response.status = response.SUCCESS
            response.robot_name = self.block_to_robot_map[block_name]
        else: 
            response.status = response.ERROR # doesn't exist
            response.robot_name = "unknown"

        # return response
        return response 

    '''
        This subscriber latches onto the OT2 state update, upon a state transfer to READY we know that the robot just finished a block and we will then delete
        that block from our dictionary marking it as finished
    '''
    def node_state_update_callback(self, msg):

        # Check state 
        if(msg.state != self.state['READY']):
            return # don't care 

        # Get block name 
        block_name = msg.block_name

        # Check if the mapping needs to be updated 
        if block_name in self.block_to_robot_map:
            self.block_to_robot_map.pop(block_name) # remove it 

        self.get_logger().warn("Block name %s processed" %(block_name,)) 

    def set_state(self, new_state):
        args = []
        args.append(self)
        args.append(new_state)
        status = retry(self, _update_scheduler_state, 10, 2, args)
        if status == self.status["ERROR"] or status == self.status["FATAL"]:
            self.get_logger().error(
                "Unable to update state with manager, continuing but the state of the ot2 may be incorrect"
            )

    '''
        Thread to distribute the "blocks" in the queue to available nodes to handle
    '''
    def run_distribute(self):
        # Runs every 3 seconds
        while rclpy.ok():
            if(self.dead == True):
                return # exit
            time.sleep(5)
            try: 
                status = self.distribute_blocks()

                if(status == self.status['ERROR']):
                    raise Exception("Unexpected Error occured in protocol_manager distribute_blocks operation")
            except Exception as e: 
                self.get_logger().error("Error occured: %r" % (e,))
                self.set_state(self.state['ERROR']) # Alert system that state is error 
                return; # exit out 
            except: # Catch other errors 
                self.set_state(self.state['ERROR']) # Alert system that state is error 
                return; # exit out 
            else: 
                if(status == self.status['FATAL']):
                    return; # Exit out we are terminating 

    def distribute_blocks(self): 
        # Termination check 
        if(self.dead == True):
            return self.status['FATAL'] # exit

        # base check
        if(len(self.protocol_queue) == 0):
            return self.status['WAITING'] # exit

        # Get nodes 
        try:
            nodes_list = get_node_list(self) # must pass in self
        except Exception as e: 
            self.get_logger().error("Unable to get nodes_list, error: %r"%(e,))
            return self.status['ERROR']
        except Exception:
            self.get_logger().error("Unable to get nodes_list")
            return self.status['ERROR']

        # Find available nodes and if we have work 
        for node in nodes_list:
            if(self.dead == True): # If termination
                return self.status['FATAL']

            # Must be correct type, if it isn't we skip 
            if(node['type'] != "OT_2"):
                continue 

            if node['state'] == self.state['READY'] and len(self.protocol_queue) > 0:
                # Get first job on queue and remove from queue 
                self.queue_lock.acquire() # enter critical section
                next_block = self.protocol_queue[0]
                block_name = next_block['block-name']
                block_tag = next_block['tag']
                protocols = next_block['tasks'].split()
                self.protocol_queue.pop(0) 
                self.queue_lock.release() 

                # Update our map 
                self.block_to_robot_map[block_name] = node['name'] # block-name to name (Switch to ID?)

                # Load/Add the Protocols
                protocol_id_list = []
                try:
                    for i in range(len(protocols)):

                        # Split protocol (for arm transfers)
                        protocol_split = protocols[i].split(":")

                        # Handle the protocol
                        if(not protocol_split[0] == 'transfer'): # Send files if not a transfer
                            protocol_id_list.append(str(load_protocols_to_ot2(self, node, protocols[i])))
                            
                        # elif protocol_split[0] == "peeler":
                        #     #CALL PEELER FUNCTIONS
                        #     # _load_peeling(peeler_name_or_id, block_name)
                        # elif protocol_split[0] == "sealer":
                        #     #CALL SEALER FUNCTIONS
                        # elif protocol_split[0] == "plate_stacker":
                        #     #CALL Plate_STACKER
                            
                        else: # Protocol is a transfer
                            protocol_split[1] = str(block_tag) + "-" + protocol_split[1]         # Add in tags 
                            protocol_split[2] = str(block_tag) + "-" + protocol_split[2]    
                            protocol_id_list.append(":".join(protocol_split))               # Recreate string

                    # Add work to OT2
                    add_work_to_ot2(self, node, protocol_id_list, block_name)               
                except Exception as e:
                    self.get_logger().error("Load/Add protocols failed to OT2: %s, Exception: %r"%(node['id'], e))
                    return self.status['ERROR']
            elif node['state'] == self.state['ERROR']: # alert at error 
                self.get_logger().warn("Node %s is in errored state and must be handled immediately!"%(node['name']))

                # Alert via some channel thing (discord bot?)

        return self.status['SUCCESS']

def main(args=None):
    rclpy.init(args=args)

    scheduler_manager_node = schedulerManager("ana")
    try:
        # Create a thread to run read_from_setup
        setup_thread = Thread(target=scheduler_manager_node.read_from_setup, args=("setup",)) #TODO: make it so you can press a button to start it
        setup_thread.start()

        # Create a thread to run run_distribute
        distribute_thread = Thread(target=scheduler_manager_node.run_distribute)
        distribute_thread.start()

        rclpy.spin(scheduler_manager_node)
    except:
        scheduler_manager_node.get_logger().error("Terminating...")

    # End
    scheduler_manager_node.dead = True
    setup_thread.join()
    distribute_thread.join()
    args = []
    args.append(scheduler_manager_node)
    status = retry(scheduler_manager_node, _deregister_node, 10, 1.5, args)  # TODO: handle status
    scheduler_manager_node.destroy_node()
    rclpy.shutdown()

if __name__ == "__main__":
    main()
