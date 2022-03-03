import rclpy
from rclpy.node import Node

# import message definitions for receiving status and position
from mavros_msgs.msg import State
from sensor_msgs.msg import NavSatFix
from std_msgs.msg import String, Float32
# import message definition for sending setpoint
from geographic_msgs.msg import GeoPoseStamped
from geometry_msgs.msg import Twist

# import service definitions for changing mode, arming, take-off and generic command
from mavros_msgs.srv import SetMode, CommandBool, CommandTOL, CommandLong
# import a_star
# from .risk_management import RiskManage

class FenswoodDroneController(Node):

    def __init__(self):
        super().__init__('controller')
        self.last_status = None     # store for last received status message
        self.last_pos = None       # store for last received position message
        self.init_alt = None       # store for global altitude at start
        self.last_alt_rel = None   # store for last altitude relative to start
        # create service clients for long command (datastream requests)...
        self.cmd_cli = self.create_client(CommandLong, '/vehicle_1/mavros/cmd/command')
        # ... for mode changes ...
        self.mode_cli = self.create_client(SetMode, '/vehicle_1/mavros/set_mode')
        # ... for arming ...
        self.arm_cli = self.create_client(CommandBool, '/vehicle_1/mavros/cmd/arming')
        # ... and for takeoff
        self.takeoff_cli = self.create_client(CommandTOL, '/vehicle_1/mavros/cmd/takeoff')
        # create publisher for setpoint
        self.target_pub = self.create_publisher(GeoPoseStamped, '/vehicle_1/mavros/setpoint_position/global', 10)
        # and make a placeholder for the last sent target
        self.last_target = GeoPoseStamped()
        # initial state for finite state machine
        # self.control_state = 'init'
        self.control_state = 'check'
        # timer for time spent in each state
        self.state_timer = 0
        # multi goal position, original: 51.4233628, -2.671761
        self.goal_position = []
        # create publisher for output risk msg to risk_management node
        self.risk_msg_pub = self.create_publisher(String, '/vehicle_1/risk_msg_input', 10)
        self.risk_msg = String()
        # create publisher for control velocity
        self.velocity_pub = self.create_publisher(Twist, '/vehicle_1/mavros/setpoint_velocity/cmd_vel_unstamped', 10)
        self.velocity = Twist()

        self.setting_alt = 20.0

    def start(self):
        # set up two subscribers, one for vehicle state...
        state_sub = self.create_subscription(State, '/vehicle_1/mavros/state', self.state_callback, 10)
        # ...and the other for global position
        pos_sub = self.create_subscription(NavSatFix, '/vehicle_1/mavros/global_position/global', self.position_callback, 10)
        # create subscriber for risk management
        risk_sub = self.create_subscription(String, '/vehicle_1/risk_alarm_state', self.risk_alarm_callback ,10)

        interface_sub = self.create_subscription(Float32, '/vehicle_1/interface_alt_output', self.interface_alt_callback, 10)

        risk_msg_output = self.create_subscription(String, '/vehicle_1/risk_msg_output', self.risk_msg_callback, 10)

        pos_list_sub = self.create_subscription(String, '/position_list', self.pos_list_callback, 10)

        # create a ROS2 timer to run the control actions
        self.timer = self.create_timer(1.0, self.timer_callback)

    def pos_list_callback(self, msg):
        init_x = -2.67155
        init_y = 51.42341
        pos_msg = msg.data.split(",")
        pos_x = (float)(pos_msg[0])
        pos_y = (float)(pos_msg[1])
        if abs(pos_x - init_x) > 0.001 or abs(pos_y - init_y) > 0.001:
            pos_xy = [pos_y, pos_x]
            self.goal_position.append(pos_xy)

    def risk_msg_callback(self, msg):
        pass
    # on receiving status message, save it to global
    def state_callback(self,msg):
        self.last_status = msg
        self.get_logger().debug('Mode: {}.  Armed: {}.  System status: {}'.format(msg.mode,msg.armed,msg.system_status))

    # on receiving positon message, save it to global
    def position_callback(self,msg):
        # determine altitude relative to start
        if self.init_alt:
            self.last_alt_rel = msg.altitude - self.init_alt
        self.last_pos = msg
        self.get_logger().debug('Drone at {}N,{}E altitude {}m'.format(msg.latitude,
                                                                        msg.longitude,
                                                                        self.last_alt_rel))

    def risk_alarm_callback(self, msg):
        if msg.data == '-1':
            self.control_state = 'init'
        if msg.data == '1':
            self.control_state = 'stop'
        if msg.data == '2':
            self.control_state = 'auto'

    def interface_alt_callback(self, msg):
        self.setting_alt = msg

    def request_data_stream(self,msg_id,msg_interval):
        cmd_req = CommandLong.Request()
        cmd_req.command = 511
        cmd_req.param1 = float(msg_id)
        cmd_req.param2 = float(msg_interval)
        future = self.cmd_cli.call_async(cmd_req)
        self.get_logger().info('Requested msg {} every {} us'.format(msg_id,msg_interval))

    def change_mode(self,new_mode):
        mode_req = SetMode.Request()
        mode_req.custom_mode = new_mode
        future = self.mode_cli.call_async(mode_req)
        self.get_logger().info('Request sent for {} mode.'.format(new_mode))

    def arm_request(self):
        arm_req = CommandBool.Request()
        arm_req.value = True
        future = self.arm_cli.call_async(arm_req)
        self.get_logger().info('Arm request sent')

    def takeoff(self,target_alt):
        takeoff_req = CommandTOL.Request()
        takeoff_req.altitude = target_alt
        future = self.takeoff_cli.call_async(takeoff_req)
        self.get_logger().info('Requested takeoff to {}m'.format(target_alt))

    def flyto(self,lat,lon,alt):
        self.last_target.pose.position.latitude = lat
        self.last_target.pose.position.longitude = lon
        self.last_target.pose.position.altitude = alt
        self.target_pub.publish(self.last_target)
        self.get_logger().info('Sent drone to {}N, {}E, altitude {}m'.format(lat,lon,alt)) 

    def state_transition(self):
        if self.control_state == 'check':
            self.risk_msg.data = 'arm check'
            self.risk_msg_pub.publish(self.risk_msg)
            return('check')

        elif self.control_state == 'stop':
            self.velocity.linear.x = float(0)
            self.velocity.linear.y = float(0)
            self.velocity.linear.z = float(0)
            self.velocity.angular.x = float(0)
            self.velocity.angular.y = float(0)
            self.velocity.angular.z = float(0)
            self.velocity_pub.publish(self.velocity)
            return('stop')
        
        elif self.control_state == 'auto':
            return('auto')

        elif self.control_state =='init':
            if self.last_status:
                if self.last_status.system_status==3:
                    self.get_logger().info('Drone initialized')
                    # send command to request regular position updates
                    self.request_data_stream(33, 1000000) # global

                    self.request_data_stream(32, 1000000)

                    # change mode to GUIDED
                    self.change_mode("GUIDED")
                    # move on to arming
                    self.risk_msg.data = 'init finished'
                    self.risk_msg_pub.publish(self.risk_msg)
                    return('arming')
                else:
                    return('init')
            else:
                return('init')

        elif self.control_state == 'arming':
            if self.last_status.armed:
                self.get_logger().info('Arming successful')
                if self.last_pos:
                    self.last_alt_rel = 0.0
                    self.init_alt = self.last_pos.altitude
                return('takeoff')
                # armed - grab init alt for relative working
            elif self.state_timer > 60:
                # timeout
                self.get_logger().error('Failed to arm')
                return('exit')
            else:
                self.arm_request()
                return('arming')

        elif self.control_state == 'takeoff':
            # send takeoff command
            if self.setting_alt:
                self.takeoff(self.setting_alt)
                return('climbing')
            elif self.state_timer > 60:
                return('exit')
            else:
                return('takeoff')

        elif self.control_state == 'climbing':
            if self.last_alt_rel > self.setting_alt - 1.0:
                self.get_logger().info('Close enough to flight altitude')
                return('goal_position_checking')
            elif self.state_timer > 60:
                # timeout
                self.get_logger().error('Failed to reach altitude')
                return('landing')
            else:
                self.get_logger().info('Climbing, altitude {}m'.format(self.last_alt_rel))
                return('climbing')

        elif self.control_state == 'goal_position_checking':
            if len(self.goal_position) == 0:
                self.get_logger().info('No goal position')
                return('landing')

            else:
                current_goal_position = self.goal_position[0]
                self.flyto(current_goal_position[0], current_goal_position[1], self.init_alt - 30.0)
                return('on_way')  

        elif self.control_state == 'on_way':
            d_lon = self.last_pos.longitude - self.last_target.pose.position.longitude
            d_lat = self.last_pos.latitude - self.last_target.pose.position.latitude
            if (abs(d_lon) < 0.0001) & (abs(d_lat) < 0.0001):
                self.get_logger().info('Close enough to target delta={},{}'.format(d_lat,d_lon))
                del self.goal_position[0]
                if len(self.goal_position) == 0:
                    return('landing')
                else:
                    return('goal_position_checking')
            elif self.state_timer > 60:
                # timeout
                self.get_logger().error('Failed to reach target')
                return('landing')
            else:
                self.get_logger().info('Target error {},{}'.format(d_lat,d_lon))
                return('on_way')

        elif self.control_state == 'volcano_nearby':
            pass

        elif self.control_state == 'landing':
            # return home and land
            # self.change_mode("RTL")
            # return('exit')
            pass

        elif self.control_state == 'exit':
            # nothing else to do
            return('exit')

    def timer_callback(self):
        new_state = self.state_transition()
        if new_state == self.control_state:
            self.state_timer = self.state_timer + 1
        else:
            self.state_timer = 0
        self.control_state = new_state
        self.get_logger().info('Controller state: {} for {} steps'.format(self.control_state, self.state_timer))

def main(args=None):
    
    rclpy.init(args=args)

    controller_node = FenswoodDroneController()
    controller_node.start()
    rclpy.spin(controller_node)


if __name__ == '__main__':
    main()