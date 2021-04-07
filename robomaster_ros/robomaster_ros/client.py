# flake8: noqa: E402
import logging

import robomaster
import rclpy.logging

# Disabled because it too expensive
# robomaster.logger = rclpy.logging.get_logger('sdk')

import robomaster.robot
import robomaster.protocol
import robomaster.conn
import robomaster.client

from robomaster_ros.modules import modules

import rclpy
import rclpy.node
import rclpy.action
import rclpy.task
import rclpy.duration

import sensor_msgs.msg

import tf2_ros.transform_broadcaster

from typing import Any, Optional


SERIAL_NUMBER_LENGTH = 14

def pad_serial(value: str) -> str:
  value = value[:SERIAL_NUMBER_LENGTH]
  value += '*' * (SERIAL_NUMBER_LENGTH - len(value))
  return value


def add_unknown_protocol(cmdset: int, cmdid: int, hint: str = '?') -> None:
    def unpack_resp(self: Any, buf: bytes, offset: int = 0) -> None:
        logging.warn(f'[{hint}] Received unknown message with cmd set {cmdset} and id {cmdid}, '
                     f'with buffer {buf!r} ({len(buf)})')
    _ = type(f'UnknownProtocol_{cmdset}_{cmdid}',
             (robomaster.protocol.ProtoData, ),
             {'_cmdset': cmdset,
              '_cmdid': cmdid,
              'unpack_resp': unpack_resp})


def add_unknown_protocols() -> None:
    # state change
    add_unknown_protocol(0x3f, 0x29, 'STATE')
    # related to uart
    add_unknown_protocol(0x3f, 0xf4, 'UART')
    # related to tof
    add_unknown_protocol(0x24, 0x21, 'TOF')


add_unknown_protocols()

# add_unknown_protocol(0x3f, 0xb3)


class FakeFtpConnection:
    def connect(self, ip: str) -> None:
        robomaster.logger.info(f"Fake FtpConnection: connect ip: {ip}")

    def upload(self, src_file: str, target_file: str) -> None:
        ...

    def stop(self) -> None:
        ...


class RoboMasterROS(rclpy.node.Node):  # type: ignore

    def __init__(self) -> None:
        super(RoboMasterROS, self).__init__("robomaster_ros")
        # robomaster.logger.set_level(logging.ERROR)
        robomaster.logger.setLevel(logging.WARN)
        conn_type: str = self.declare_parameter("conn_type", "sta").value[:]
        sn: Optional[str] = self.declare_parameter("serial_number", None).value
        if sn:
            if len(sn) != SERIAL_NUMBER_LENGTH:
                sn = pad_serial(sn)
                self.get_logger().warn(
                    f"Serial number must have length {SERIAL_NUMBER_LENGTH}: "
                    f"trasformed to {sn}")
        else:
            sn = None
        robomaster.conn.FtpConnection = FakeFtpConnection
        self.ep_robot = robomaster.robot.Robot()
        self.disconnection = rclpy.task.Future(executor=rclpy.get_global_executor())
        # For now, to handle simulations without FTP
        self.initialized = False
        self.get_logger().info(f"Try to connect via {conn_type} to robot with sn {sn}")
        try:
            self.ep_robot.initialize(conn_type=conn_type, sn=sn)
        except (AttributeError, TypeError):
            self.get_logger().error("Could not connect")
            self.disconnection.set_result(False)
            return

        self._tf_name = self.declare_parameter('tf_prefix', '').value

        self.initialized = True
        self._got_hb = False
        self.heartbeat_check_timer = self.create_timer(3, self.heartbeat_check)
        self.heartbeat_handler = robomaster.client.MsgHandler(
            proto_data=robomaster.protocol.ProtoSdkHeartBeat(),
            ack_cb=lambda _, msg: self.got_heart_beat(msg))

        self.ep_robot._client.add_msg_handler(self.heartbeat_handler)
        self.joint_state_pub = self.create_publisher(
            sensor_msgs.msg.JointState, 'joint_states_p', 1)
        self.tf_broadcaster = tf2_ros.transform_broadcaster.TransformBroadcaster(self)
        self.modules = [module(self.ep_robot, self) for name, module in
                        modules.items() if self.enabled(name)]
        module_string = ', '.join(type(module).__name__ for module in self.modules)
        self.get_logger().info(f"Enabled modules: {module_string}")

    def __del__(self) -> None:
        self.get_logger().info("Terminate client")
        if self.initialized:
            for module in self.modules:
                module.stop()
            self.ep_robot.close()

    def tf_frame(self, name: str) -> str:
        if self._tf_name:
            return f'{self._tf_name}/{name}'
        return name

    def enabled(self, name: str) -> bool:
        return self.declare_parameter(f"{name}.enabled", False).value

    def heartbeat_check(self) -> None:
        self.heartbeat_check_timer.reset()
        # print('heartbeat_check', time.time(), self._got_hb)
        if not self._got_hb:
            self.disconnection.set_result(True)
            self.get_logger().warn("Disconnected")
            self.get_logger().warn(f"{self.disconnection.done()}")
        self._got_hb = False

    def got_heart_beat(self, msg: Any) -> None:
        # print('H', time.time())
        self._got_hb = True
