"""Terminal keyboard -> existing Office Sim /a2_control joystick driver.

ROS imports are lazy. This module never calls the planner, localization or Nav2.
"""
import argparse
from collections import deque
import math
import select
import signal
import sys
import termios
import time
import tty


class KeyboardState:
    def __init__(self,speed=.3,strafe=.2,turn=.5,release_timeout=.3):
        for name,value,maximum in [('speed',speed,.7),('strafe',strafe,.4),('turn',turn,1.2),('release_timeout',release_timeout,.5)]:
            if not math.isfinite(value) or not 0<value<=maximum:raise ValueError(f'{name} must be positive and <= {maximum}')
        self.speed,self.strafe,self.turn=speed,strafe,turn
        self.release_timeout=release_timeout
        self.last=-math.inf;self.command=(0.,0.,0.)

    def feed(self,key,now):
        moves={'w':(self.speed,0.,0.),'s':(-self.speed,0.,0.),'a':(0.,0.,self.turn),
               'd':(0.,0.,-self.turn),'q':(0.,self.strafe,0.),'e':(0.,-self.strafe,0.),' ':(0.,0.,0.)}
        if key.lower() in moves:self.command=moves[key.lower()];self.last=now

    def velocity(self,now):
        return self.command if 0<=now-self.last<self.release_timeout else (0.,0.,0.)


def encode_axes(velocity,params):
    """Invert the installed joy_teleop mapping, including its deadzone."""
    dead=params['deadzone']
    if not math.isfinite(dead) or not 0<=dead<1:raise ValueError('invalid live deadzone')
    indices=[params['axis_'+n] for n in ('vx','vy','yaw')]
    if any(type(i) is not int or not 0<=i<16 for i in indices) or len(set(indices))!=3:
        raise ValueError('invalid or overlapping live axes')
    axes=[0.]*max(4,max(indices)+1)
    for value,name,cap_name,index in zip(velocity,('vx','vy','yaw'),('vx_max','vy_max','wz_max'),indices):
        cap=params[cap_name];sign=params['sign_'+name]
        if not math.isfinite(cap) or cap<=0 or sign not in (-1.,1.):raise ValueError('invalid live speed cap or sign')
        if not math.isfinite(value) or abs(value)>cap:raise ValueError('requested speed exceeds live joystick cap')
        if value:axes[index]=sign*math.copysign(dead+(1.-dead)*abs(value)/cap,value)
    return axes


PARAM_NAMES=['vx_max','vy_max','wz_max','axis_vx','axis_vy','axis_yaw','sign_vx','sign_vy','sign_yaw','deadzone']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--speed',type=float,default=.3,help='forward/reverse m/s, default 0.3')
    parser.add_argument('--strafe',type=float,default=.2,help='sideways m/s, default 0.2')
    parser.add_argument('--turn',type=float,default=.5,help='yaw rad/s, default 0.5')
    parser.add_argument('--release-timeout',type=float,default=.3,help='wall seconds since last key repeat; max 0.5')
    parser.add_argument('--check',action='store_true',help='read-only simulation/joystick readiness; publish nothing')
    args=parser.parse_args()
    try:keys=KeyboardState(args.speed,args.strafe,args.turn,args.release_timeout)
    except ValueError as exc:parser.error(str(exc))
    if not args.check and not sys.stdin.isatty():parser.error('interactive terminal required; use --check for a read-only check')
    import os
    if os.environ.get('ROS_DOMAIN_ID')!='72':parser.error('this controller is restricted to Office Sim ROS domain 72')
    import rclpy
    from rclpy.signals import SignalHandlerOptions
    from sensor_msgs.msg import Joy
    from rosgraph_msgs.msg import Clock
    from rcl_interfaces.srv import GetParameters
    from rcl_interfaces.msg import ParameterType
    rclpy.init(args=[],signal_handler_options=SignalHandlerOptions.NO)
    node=rclpy.create_node('coegent_office_keyboard')
    pub=node.create_publisher(Joy,'/a2_control',10)
    samples=deque(maxlen=2)
    node.create_subscription(Clock,'/clock',lambda m:samples.append((m.clock.sec+m.clock.nanosec*1e-9,time.monotonic())),10)
    sent=False;terminal=None;interrupted=False;previous={}
    def stop_signal(signum,frame):
        nonlocal interrupted
        interrupted=True
    def send(velocity,params):
        nonlocal sent
        msg=Joy();msg.header.stamp=node.get_clock().now().to_msg();msg.header.frame_id='control_link'
        msg.axes=encode_axes(velocity,params);msg.buttons=[0,0,0,0]
        pub.publish(msg);sent=True
    try:
        for sig in (signal.SIGINT,signal.SIGTERM):previous[sig]=signal.signal(sig,stop_signal)
        until=time.monotonic()+6
        while time.monotonic()<until and not interrupted:
            rclpy.spin_once(node,timeout_sec=.05)
            names=[n for n,namespace in node.get_node_names_and_namespaces()]
            if 'teamgrit_isaac_planar_sim' in names and 'joy_teleop' in names and pub.get_subscription_count() and len(samples)>1 and samples[-1][0]>samples[0][0]:break
        else:raise RuntimeError('Office Sim clock/node or /a2_control joystick subscriber unavailable')
        client=node.create_client(GetParameters,'/joy_teleop/get_parameters')
        if not client.wait_for_service(timeout_sec=3.):raise RuntimeError('cannot verify actual joystick speed/axis mapping')
        future=client.call_async(GetParameters.Request(names=PARAM_NAMES))
        until=time.monotonic()+3
        while not future.done() and time.monotonic()<until and not interrupted:rclpy.spin_once(node,timeout_sec=.05)
        if not future.done() or future.result() is None:raise RuntimeError('joystick parameter query timed out')
        values=future.result().values
        if len(values)!=len(PARAM_NAMES):raise RuntimeError('invalid joystick parameter response')
        params={name:(v.integer_value if v.type==ParameterType.PARAMETER_INTEGER else v.double_value if v.type==ParameterType.PARAMETER_DOUBLE else math.nan)
                for name,v in zip(PARAM_NAMES,values)}
        encode_axes((args.speed,args.strafe,args.turn),params)
        print('Office Sim ready: domain72 /a2_control -> joy_teleop -> existing actuation -> office_sim',flush=True)
        print('Live joystick caps (m/s, rad/s):',params['vx_max'],params['vy_max'],params['wz_max'],flush=True)
        if args.check:
            print('CHECK PASSED: no joystick messages published',flush=True);return 0
        print('Focus THIS terminal. W/S forward/reverse; A/D turn; Q/E sidestep; Space stop; X or Esc exit.',flush=True)
        print(f'Hold a key; terminal repeats renew motion. No repeat for {args.release_timeout:.2f}s sends stop. First repeat may pause briefly.',flush=True)
        terminal=termios.tcgetattr(sys.stdin.fileno());tty.setcbreak(sys.stdin.fileno())
        active=False;next_send=0.
        while rclpy.ok() and not interrupted:
            rclpy.spin_once(node,timeout_sec=0.)
            if select.select([sys.stdin],[],[],.02)[0]:
                chars=os.read(sys.stdin.fileno(),128).decode(errors='ignore')
                if not chars:break
                for char in chars:
                    if char.lower()=='x' or char in ('\x1b','\x03','\x04'):
                        interrupted=True;break
                    keys.feed(char,time.monotonic())
                    if char==' ':
                        send((0.,0.,0.),params);active=False
                if interrupted:break
            now=time.monotonic();velocity=keys.velocity(now);moving=any(velocity)
            if moving and now>=next_send:
                if now-samples[-1][1]>2:raise RuntimeError('Office Sim clock stream lost')
                send(velocity,params);active=True;next_send=now+.08
            elif not moving and active:
                send((0.,0.,0.),params);active=False;print('\rSTOP                 ',end='',flush=True)
        return 0
    except (RuntimeError,ValueError) as exc:
        print('Keyboard controller:',exc,file=sys.stderr);return 2
    finally:
        if terminal is not None:termios.tcsetattr(sys.stdin.fileno(),termios.TCSADRAIN,terminal)
        if sent and rclpy.ok():
            # Best-effort repeated centred frames; the existing joystick and simulator watchdogs remain active.
            msg=Joy();msg.axes=[0.]*max(4,len(msg.axes));msg.buttons=[0,0,0,0]
            for _ in range(3):pub.publish(msg);rclpy.spin_once(node,timeout_sec=.04)
            print('\nCentred joystick sent; controller closed.',flush=True)
        node.destroy_node();rclpy.try_shutdown()
        for sig,handler in previous.items():signal.signal(sig,handler)


if __name__=='__main__':raise SystemExit(main())
