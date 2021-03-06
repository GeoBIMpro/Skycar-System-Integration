#!/usr/bin/env python

# Standard imports
import math
import yaml
import time

# Dependecy imports
import numpy as np
import rospy
from std_msgs.msg import Int32
from geometry_msgs.msg import PoseStamped
from sensor_msgs.msg import Image
from cv_bridge import CvBridge
import cv2

# Local imports
from light_classification.tl_classifier import TLClassifier
from styx_msgs.msg import TrafficLightArray, TrafficLight # pylint: disable=E0401
from styx_msgs.msg import Lane # pylint: disable=E0401
import tf
import yaml
import math
import time

STATE_COUNT_THRESHOLD = 3

USE_GROUND_TRUTH = False         # Use the ground truth traffic light data on /vehicle/traffic_lights
                                 # This is to allow a build of the final waypoint controllers before
                                 #   the traffic light classification has been developed

# Parameters used when gathering example images for TL classifier training
GATHER_IMAGES = False           # Set to true to gather sample images
GATHER_DISTANCE_MAX = 150.0     # Distance to light at which to start gathering sample images
GATHER_DISTANCE_MIN = 20.0      # Distance to light at which to stop gathering sample images
GATHER_RED = False              # Gather red light examples
GATHER_YELLOW = True            # Gather yellow light examples
GATHER_GREEN = True             # Gather green light examples
GATHER_RATE = 2                 # Will take the counter mod this value, to gather frames without slamming CPU
GATHER_DATA_PATH = '/home/neil/Downloads/sim_ground_truth'     # where to put the gathered images (edit for local)

class TLDetector(object):
    """Traffic Light detection and classifaction. Results publishing to ROS nodes."""

    def __init__(self):
        rospy.init_node('tl_detector')

        self.pose = None
        self.waypoints = None
        self.camera_image = None
        self.lights = []

        # Load classifier before starting substribers because it takes some time
        self.light_classifier = TLClassifier(sim=False)

        _ = rospy.Subscriber('/current_pose', PoseStamped, callback=self.pose_cb)
        self.base_waypoints_sub = rospy.Subscriber('/base_waypoints', Lane,
                                                   callback=self.waypoints_cb)
        self.traffic_light_sub = rospy.Subscriber('/vehicle/traffic_lights',
                                                  TrafficLightArray, callback=self.traffic_cb)
        # provides an image stream from the car's camera. These images are used to determine the
        # color of upcoming traffic lights.
        _ = rospy.Subscriber('/image_color', Image, self.image_cb)

        config_string = rospy.get_param("/traffic_light_config")
        self.config = yaml.load(config_string)

        self.upcoming_red_light_pub = rospy.Publisher('/traffic_waypoint', Int32, queue_size=1)

        self.bridge = CvBridge()

        self.listener = tf.TransformListener()

        self.state = TrafficLight.UNKNOWN
        self.last_state = TrafficLight.UNKNOWN
        self.last_wp = -1
        self.state_count = 0

        self.has_image = None
        self.prev_light_loc = None

        self.counter = 0

        rospy.loginfo('Red: %s', TrafficLight.RED)
        rospy.loginfo('Yellow: %s', TrafficLight.YELLOW)
        rospy.loginfo('Green: %s', TrafficLight.GREEN)
        rospy.loginfo('Unknown: %s', TrafficLight.UNKNOWN)

        self.gather_count = 0           # used to skip frames when gathering ground truth data

        self.stop_line_positions = self.config['stop_line_positions']
        self.stop_line_wp = []

        rospy.spin()

    def distance(self, pt1, pt2):
        """Calculates dinstace from one point to another in 2D"""
        delta_x = pt1.x - pt2.x
        delta_y = pt1.y - pt2.y
        return math.sqrt(delta_x * delta_x + delta_y * delta_y)

    def pose_cb(self, msg):
        """Callback fuction for vehicle's location subscription."""
        self.pose = msg

    def waypoints_cb(self, waypoints):
        """Callback fuction for complete list of waypoints."""
        self.waypoints = waypoints
        # update the light positions - do this once here, so that we can use it or each TL detection
        self.stop_line_wp = []
        for lpts in self.stop_line_positions:
            l_pos = self.get_closest_waypoint_to_point(lpts[0], lpts[1], self.waypoints)
            self.stop_line_wp.append(l_pos)

    def get_closest_waypoint_to_point(self, pos_x, pos_y, waypoints):
        """Identifies the closest path waypoint to the given position
           https://en.wikipedia.org/wiki/Closest_pair_of_points_problem
        Args:
            pose (Pose): position to match a waypoint to
        Returns:
            int: index of the closest waypoint in self.waypoints
        """

        best_waypoint = None

        def distance2D(x1, y1, x2, y2):
            """Calculates dinstace from one point to another in 2D"""
            delta_x = x1 - x2
            delta_y = y1 - y2
            return math.sqrt(delta_x * delta_x + delta_y * delta_y)

        wps = waypoints.waypoints
        min_dist = distance2D(pos_x, pos_y,
                                 wps[0].pose.pose.position.x,
                                 wps[0].pose.pose.position.y)

        for i, point in enumerate(wps):
            dist = distance2D(pos_x, pos_y,
                                 point.pose.pose.position.x,
                                 point.pose.pose.position.y)
            if dist < min_dist:
                best_waypoint = i
                min_dist = dist

        return best_waypoint

        # we only need the message once, unsubscribe as soon as we handled the message
        self.base_waypoints_sub.unregister()

    def traffic_cb(self, msg):
        """
        /vehicle/traffic_lights helps you acquire an accurate ground truth data source for the
        traffic light classifier, providing the location and current color state of all traffic
        lights in the simulator. This state can be used to generate classified images or subbed
        into your solution to help you work on another single component of the node. This topic
        won't be available when testing your solution in real life so don't rely on it in the
        final submission.
        """
        self.lights = msg.lights

    def image_cb(self, msg):
        """Identifies red lights in the incoming camera image and publishes the index
            of the waypoint closest to the red light's stop line to /traffic_waypoint
        Args:
            msg (Image): image from car-mounted camera

        """
        self.has_image = True
        self.camera_image = msg

        if USE_GROUND_TRUTH:
            light_wp, state = self.process_ground_truth_lights()

            if GATHER_IMAGES:
                # Gather some ground truth data here
                if (self.pose) and light_wp > 0:
                    # Are we close enough?
                    ld = self.distance(self.pose.pose.position, self.waypoints.waypoints[light_wp].pose.pose.position)
                    if ld <= (GATHER_DISTANCE_MAX) and (ld >= GATHER_DISTANCE_MIN):
                        # get the image data
                        self.gather_count += 1
                        if (self.gather_count % GATHER_RATE) == 0:
                            fwrite = False
                            gather_dir = GATHER_DATA_PATH + '/'
                            if (state == TrafficLight.RED) and GATHER_RED:
                                gather_dir += 'RED/'
                                fwrite = True
                            elif (state == TrafficLight.YELLOW) and GATHER_YELLOW:
                                gather_dir += 'YELLOW/'
                                fwrite = True
                            elif (state == TrafficLight.GREEN) and GATHER_GREEN:
                                gather_dir += 'GREEN/'
                                fwrite = True
                            if fwrite:
                                # write out the file
                                cv_image = self.bridge.imgmsg_to_cv2(self.camera_image, "bgr8")
                                cv_image = cv2.resize(cv_image, (400, 300))
                                cv2.imwrite(gather_dir + '{}.png'.format(time.time()), cv_image)
                                rospy.loginfo("[test] Gathering ground truth for state %s (%s)", state, self.gather_count)


        else:
            light_wp, state = self.process_traffic_lights()

        rospy.loginfo("Lights state %s", state)

        '''
        Publish upcoming red lights at camera frequency.
        Each predicted state has to occur `STATE_COUNT_THRESHOLD` number
        of times till we start using it. Otherwise the previous stable state is
        used.
        '''
        if self.state != state:
            self.state_count = 0
            self.state = state
        elif self.state_count >= STATE_COUNT_THRESHOLD:
            self.last_state = self.state
            light_wp = light_wp if state == TrafficLight.RED else -1
            self.last_wp = light_wp

            self.upcoming_red_light_pub.publish(Int32(light_wp))
        else:
            self.upcoming_red_light_pub.publish(Int32(self.last_wp))

        self.state_count += 1

    def get_closest_waypoint(self, pos_x, pos_y, waypoints):
        """Identifies the closest path waypoint to the given position
           https://en.wikipedia.org/wiki/Closest_pair_of_points_problem
        Args:
            pose (Pose): position to match a waypoint to

        Returns:
            int: index of the closest waypoint in self.waypoints

        """

        best_waypoint = None

        wps = waypoints.waypoints
        min_dist = self.distance(pos_x, pos_y,
                                 wps[0].pose.pose.position.x,
                                 wps[0].pose.pose.position.y)

        for i, point in enumerate(wps):
            dist = self.distance(pos_x, pos_y,
                                 point.pose.pose.position.x,
                                 point.pose.pose.position.y)
            if dist < min_dist:
                best_waypoint = i
                min_dist = dist

        return best_waypoint

    def distance(self, pt1_x, pt1_y, pt2_x, pt2_y):
        """Calculates dinstace from one point to another in 2D"""
        delta_x = pt1_x - pt2_x
        delta_y = pt1_y - pt2_y
        return math.sqrt(delta_x * delta_x + delta_y * delta_y)

    def project_to_image_plane(self, point_in_world):
        """Project point from 3D world coordinates to 2D camera image location

        Args:
            point_in_world (Point): 3D location of a point in the world

        Returns:
            x (int): x coordinate of target point in image
            y (int): y coordinate of target point in image

        """

        f_x = self.config['camera_info']['focal_length_x']
        f_y = self.config['camera_info']['focal_length_y']
        image_width = self.config['camera_info']['image_width']
        image_height = self.config['camera_info']['image_height']

        cord_x = point_in_world[0]
        cord_y = point_in_world[1]

        # get transform between pose of camera and world frame
        # trans = None
        # try:
        #     now = rospy.Time.now()
        #     self.listener.waitForTransform("/base_link", "/world", now, rospy.Duration(1.0))
        #     (trans, rot) = self.listener.lookupTransform("/base_link", "/world", now)

        # except (tf.Exception, tf.LookupException, tf.ConnectivityException):
        #     rospy.logerr("Failed to find camera to map transform")

        # From quaternion to Euler angles:
        pose_x = self.pose.pose.orientation.x
        pose_y = self.pose.pose.orientation.y
        pose_z = self.pose.pose.orientation.z
        pose_w = self.pose.pose.orientation.w

        # Determine car heading:
        t_3 = 2.0 * (pose_w * pose_z + pose_x * pose_y)
        t_4 = 1.0 - 2.0 * (pose_y * pose_y + pose_z * pose_z)
        theta = math.degrees(math.atan2(t_3, t_4))

        x_car = ((cord_y - self.pose.pose.position.y) *
                 math.sin(math.radians(theta)) -
                 (self.pose.pose.position.x - cord_x) *
                 math.cos(math.radians(theta)))
        y_car = ((cord_y-self.pose.pose.position.y) *
                 math.cos(math.radians(theta)) -
                 (cord_x - self.pose.pose.position.x) *
                 math.sin(math.radians(theta)))

        obj_points = np.array([[float(x_car), float(y_car), 0.0]], dtype=np.float32)

        rvec = (0, 0, 0)
        tvec = (0, 0, 0)

        camera_matrix = np.array([[f_x, 0, image_width / 2],
                                  [0, f_y, image_height / 2],
                                  [0, 0, 1]])
        dist_coeffs = None


        ret, _ = cv2.projectPoints(obj_points, rvec, tvec, camera_matrix, dist_coeffs)

        light_x = int(ret[0, 0, 0])
        light_y = int(ret[0, 0, 1])

        return (light_x, light_y)

    def get_light_state(self, light):
        """Determines the current color of the traffic light

        Args:
            light (TrafficLight): light to classify

        Returns:
            int: ID of traffic light color (specified in styx_msgs/TrafficLight)

        """
        if not self.has_image:
            self.prev_light_loc = None
            return False

        # self.camera_image.encoding = "rgb8"
        cv_image = self.bridge.imgmsg_to_cv2(self.camera_image, "rgb8")

        light_x, light_y = self.project_to_image_plane(light)

        if ((light_x is None) or (light_y is None) or (light_x < 0) or (light_y < 0) or
                (light_x > self.config['camera_info']['image_width']) or
                (light_y > self.config['camera_info']['image_height'])):
            return TrafficLight.UNKNOWN
        else:
            # Cropped for Vladimir's trained simulaotr images

            # Downsample image for faster processing
            cv_image = cv2.resize(cv_image, (400, 300))

            cv2.imwrite('/home/nauris/Downloads/sim_data/{}.png'.format(time.time()), cv_image)
            start_time = time.time()
            state = self.light_classifier.get_classification(cv_image)
            rospy.loginfo("Processed in %.4f with sate: %s", time.time() - start_time, state)

            return state

    def process_traffic_lights(self):
        """Finds closest visible traffic light, if one exists, and determines its
           location and color.all

        Returns:
            int: index of waypoint closest to the upcoming traffic light (-1 if none exists)
            int: ID of traffic light color (specified in styx_msgs/TrafficLight)

        """
        light_positions = self.config['stop_line_positions']

        light = None
        car_position = None
        light_pos_wp = None
        light_wp = None
        lane_distance = None

        if self.pose and self.waypoints is not None:
            # Closest waypoint to current state
            car_position = self.get_closest_waypoint(self.pose.pose.position.x,
                                                     self.pose.pose.position.y,
                                                     self.waypoints)

            light_pos_wp = []
            for lpts in light_positions:
                l_pos = self.get_closest_waypoint(lpts[0], lpts[1], self.waypoints)
                light_pos_wp.append(l_pos)

            # get id of next light
            if car_position > max(light_pos_wp):
                light_wp = min(light_pos_wp)
            else:
                light_delta = light_pos_wp[:]
                light_delta[:] = [x - car_position for x in light_delta]
                light_wp = min(i for i in light_delta if i > 0) + car_position

            light_idx = light_pos_wp.index(light_wp)
            light = light_positions[light_idx]

            lane_distance = self.distance(
                light[0],
                light[1],
                self.waypoints.waypoints[car_position].pose.pose.position.x,
                self.waypoints.waypoints[car_position].pose.pose.position.y
            )

        # rospy.loginfo("[test] car_position: %s", car_position)
        # rospy.loginfo("[test] light_pos_wp: %s", light_pos_wp)
        # rospy.loginfo("[test] next_light_pos: %s", light_wp)
        # rospy.loginfo("[test] light: %s", light)
        # rospy.loginfo("[test] light_distance: %s", lane_distance)

        if light:
            state = self.get_light_state(light)

            if state == TrafficLight.RED:
                return light_wp, state
            else:
                return light_wp, -1
        # self.waypoints = None
        return -1, TrafficLight.UNKNOWN

    def process_ground_truth_lights(self):
        """
        Finds the closest traffic light in the list of ground truth lights,
        then returns the state for that light.

        Returns:
            int: index of waypoint closes to the upcoming traffic light (-1 if none exists)
            int: ID of traffic light color (specified in styx_msgs/TrafficLight)

        """
        #light = None
        # stop_line_positions = self.config['stop_line_positions']
        next_traffic_light_waypoint = -1
        next_traffic_light_state = TrafficLight.UNKNOWN

        if self.pose:

            car_position = self.get_closest_waypoint(self.pose.pose.position.x,
                                                     self.pose.pose.position.y,
                                                     self.waypoints)

            if car_position:
                for _light in self.lights:

                    waypoint_nearest_light = self.get_closest_waypoint(_light.pose.pose.position.x,
                                                                       _light.pose.pose.position.y,
                                                                       self.waypoints)

                    #rospy.loginfo("[test] light waypoint: %s", waypoint_nearest_light)
                    if waypoint_nearest_light > car_position:
                        next_traffic_light_waypoint = waypoint_nearest_light
                        next_traffic_light_state = _light.state
                        break

        best_stop_line = -1
        if next_traffic_light_waypoint > 0:
            # find the stop line location nearest to this traffic light
            stop_distance = 10000           # here distance is just a count of waypoints, not meters
            for stop_line in self.stop_line_wp:
                if stop_line < next_traffic_light_waypoint:
                    # stop line is before the traffic light
                    if (next_traffic_light_waypoint - stop_line) < stop_distance:
                        stop_distance = (next_traffic_light_waypoint - stop_line)
                        best_stop_line = stop_line

        if best_stop_line > 0:
            return best_stop_line, next_traffic_light_state
        else:
            return -1, TrafficLight.UNKNOWN

if __name__ == '__main__':
    try:
        TLDetector()
    except rospy.ROSInterruptException:
        rospy.logerr('Could not start traffic node.')
