# ROS libraries 
import rclpy
from rclpy.node import Node

# Time Library
import time

# ROS messages and services
from workcell_interfaces.srv import *
from workcell_interfaces.msg import *

'''
    Calls the service to update the respective scheduler's state, this is currently set up so that only the scheduler is able to call this function. 
'''
def update_scheduler_state(self, current_state):

    # Error checking
    if not (current_state in self.state.values()):
        return self.status["ERROR"]  # Error

    # Create a request
    msg = SchStateUpdate()
    msg.state = current_state
    msg.id = self.id

    # Create client and wait for service
    sch_state_update_pub = self.create_publisher(
        SchStateUpdate, "/sch/sch_state_update", 10
    )
    time.sleep(1)  # wait for it to start

    # Call client
    sch_state_update_pub.publish(msg)

    # No error checks without services
    return self.status["SUCCESS"]

# Middleman function to segway from retry functions to update_scheduler_state
def _update_scheduler_state(args):
    return update_scheduler_state(args[0], args[1])  # self, current_state

def main_null():
    print("This is not meant to have a main function")
