#!/usr/bin/python3

import platform
import jetson_utils
import jetson_inference
import sys
import configparser
import sys
import time
from datetime import datetime
import json
import logging
import sqlite3
import paho.mqtt.client as mqtt
import shutil
import os
from PIL import Image, ImageFont, ImageDraw, ImageEnhance
import collections
import numpy
import email, smtplib, ssl
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

cameras = {}
camera_ip_address = {}
rtsp_streams = {}
object_mutelist_inside = {}
object_mutelist_outside = {}
basic_stats = {}
image_ai = {}
image_bw = {}
resized_image_bw = {}
CudaImageBuffers = {}

config = None
smtp_down = False

def read_config(config_file):

	global config
	config = configparser.ConfigParser()
	config.read(sys.argv[1])
	
	global cameras
	cameras = dict(config['camerasAI']) 

	global using_rtsp_simple_proxy
	using_rtsp_simple_proxy = int(config["general"]["using_rtsp_simple_proxy"])

	global object_mutelist_inside
	for camera_details, uri in cameras.items():
		camera, type = camera_details.split(',')
		mutelist_key="object_mutelist_inside-" + camera 
		try:
			object_mutelist_inside[camera] = dict(config[mutelist_key])
		except:
			pass

	global object_mutelist_outside
	for camera_details, uri in cameras.items(): 
		camera, type = camera_details.split(',')
		mutelist_key="object_mutelist_outside-" + camera 
		try:
			object_mutelist_outside[camera] = dict(config[mutelist_key])
		except:
			pass

	global label_mutelist
	try:
		label_mutelist = config["label_mutelist"]["labels"]
	except:
		label_mutelist = ""

	global ai_resize_factor
	ai_resize_factor = float(config["general"]["ai_resize_factor"])

	global camera_down_timeout_ms
	camera_down_timeout_ms = int(config["general"]["camera_down_timeout_ms"])

	global camera_attempt_restart_timer
	camera_attempt_restart_timer = int(config["general"]["camera_attempt_restart_timer"])

	global camera_starting_up_timeout
	camera_starting_up_timeout = int(config["general"]["camera_starting_up_timeout"])

	global label_alarmlist_confidence_override
	label_alarmlist_confidence_override = dict(config['label_alarmlist_confidence_override'])

	global label_percent_screenfill_min
	label_percent_screenfill_min = dict(config['label_percent_screenfill_min'])

	global image_storage_dir
	image_storage_dir = config["general"]["image_storage_dir"]

	global label_file_name
	label_file_name = config["general"]["label_filename"]

	global baseline_model_confidence
	baseline_model_confidence = float(config["general"]["baseline_model_confidence"])

	global label_name_index_array
	with open(label_file_name, "r") as filehandle:
		label_name_index_array = filehandle.read().splitlines()

	global logfile
	logfile = config["general"]["logfile"]

	global sqlite_db 
	sqlite_db = config["general"]["sqlite_db"]

	global video_storage_dir
	video_storage_dir = config["general"]["video_storage_dir"]

	global stats_update_seconds
	stats_update_seconds = int(config["general"]["stats_update_seconds"])

	global mutelist_reminder_folder
	mutelist_reminder_folder = config["general"]["mutelist_reminder_folder"]

	global logging_level_debug
	logging_level_debug = int(config["general"]["logging_level_debug"])

	global motion_resize_factor
	motion_resize_factor = float(config["motion_detect_settings"]["motion_resize_factor"])

	global frame_check_delta
	frame_check_delta = int(config["motion_detect_settings"]["frame_check_delta"])

	global movement_delta_threshold
	movement_delta_threshold = int(config["motion_detect_settings"]["movement_delta_threshold"])

	global movement_hits_threshold_percent 
	movement_hits_threshold_percent = float(config["motion_detect_settings"]["movement_hits_threshold_percent"])

	try:
		global mqtt_ip_address
		mqtt_ip_address = config["mqtt"]["mqtt_ip_address"]

		global mqtt_username 
		mqtt_username = config["mqtt"]["mqtt_username"]

		global mqtt_password
		mqtt_password = config["mqtt"]["mqtt_password"]

		global mqtt_topic
		mqtt_topic = config["mqtt"]["mqtt_topic"]
	except:
		pass


	try:
		global smtp_update_seconds
		smtp_update_seconds = 60

		smtp_update_seconds = int(config["smtp"]["smtp_update_seconds"])

		global sender_email
		sender_email = config["smtp"]["sender_email"]

		global receiver_email
		receiver_email= config["smtp"]["receiver_email"]

		global smtp_password
		smtp_password= config["smtp"]["smtp_password"]

	except:
		pass

def test_event_needs_filtered(camera, eventclass, left, right, top, bottom, percent_screenfill): 
	
	global label_percent_screenfill_min

	if eventclass in label_mutelist:
		return eventclass

	try:
		if percent_screenfill < float(label_percent_screenfill_min[eventclass]):
			return "percent_screenfill"
	except:
		pass

	try:
		my_mutellist =  object_mutelist_inside[camera]
		for name, mutelist_coords in my_mutellist.items():
			mute_left,mute_right,mute_top,mute_bottom = mutelist_coords.split(',')
			mute_left = int(mute_left)
			mute_right = int(mute_right)
			mute_top = int(mute_top)
			mute_bottom = int(mute_bottom)
					
			if ( (left >= mute_left) and (right <= mute_right) and (top >= mute_top) and (bottom <= mute_bottom) ):
				return name
	except:
		pass

	try:
		my_mutellist =  object_mutelist_outside[camera]
		for name, mutelist_coords in my_mutellist.items():
			mute_left,mute_right,mute_top,mute_bottom = mutelist_coords.split(',')
			mute_left = int(mute_left)
			mute_right = int(mute_right)
			mute_top = int(mute_top)
			mute_bottom = int(mute_bottom)
					
			if ( (left < mute_left) and (right > mute_right) and (top < mute_top) and (bottom > mute_bottom) ):
				return name
	except:
		pass

	return ""

class BasicStatsAgainstThreshold:

	def __init__(self, threshold):
		self._threshold = threshold
		self._minumum = sys.float_info.max
		self._maximum = 0.0
		self._total = 0.0
		self._calls = 0
		self._count_events_exceeding_threshold = 0

	def reset(self):
		if self._threshold is None:
			return None
		self._minumum = 0.0
		self._maximum = 0.0
		self._total = 0.0
		self._calls = 0
		self._count_events_exceeding_threshold = 0

	def update(self, value):
		if self._threshold is None:
			return None
		self._calls = self._calls + 1	
		self._total = self._total + value
		if value < self._minumum:
			self._minumum = value
		if value > self._maximum:
			self._maximum = value
		if value > self._threshold:
			self._count_events_exceeding_threshold = self._count_events_exceeding_threshold + 1
	
	def getstats(self): 
		if self._calls > 0:
			average = self._total / self._calls
		else:
			average = 0.0

		return self._minumum, self._maximum, average , self._count_events_exceeding_threshold, self._calls, self._threshold

class TimeoutCheck:

	def __init__(self, seconds_to_expire):
		self._start_time = 0
		self._seconds_to_expire = seconds_to_expire

	def start(self):
		self._start_time = time.perf_counter()

	def expired(self):
		elapsed_time = time.perf_counter() - self._start_time
		if elapsed_time > self._seconds_to_expire:
			self._start_time = time.perf_counter()
			return True
		else:
			return False

class FrameBuffer:

	def __init__(self, number_of_frames, width, height, format):
		self._total_number_of_frames = number_of_frames
		self._index = 0
		self._frames_added = 0
		self._frame = []
		for index in range(number_of_frames):
			image_buffer = jetson_utils.cudaAllocMapped(width = width, height = height, format = format)
			self._frame.append(image_buffer)

	def add_frame(self):
		retval = self._index
		self._frames_added = self._frames_added + 1
		self._index = self._index + 1
		if self._index >= self._total_number_of_frames:
			self._index = 0
		return self._frame[retval]

	def get_historic_frame(self, frames_back):
		if frames_back > self._frames_added:
			return None
		if frames_back > self._total_number_of_frames:
			return None
		index = self._index - frames_back
		if index < 0:
			index = index + self._total_number_of_frames
		return self._frame[index]

def is_motion_detected(camera, image):

	global CudaImageBuffers
	global frame_check_delta
	global movement_delta_threshold 
	global movement_hits_threshold_percent 
	global basic_stats
	global motion_resize_factor

	jetson_utils.cudaConvertColor(image, image_bw[camera])

	jetson_utils.cudaResize(image_bw[camera], resized_image_bw[camera])

	jetson_utils.cudaResize(image_bw[camera], CudaImageBuffers[camera].add_frame())

	image_old = CudaImageBuffers[camera].get_historic_frame(1)
	if image_old:
			numpy_old = jetson_utils.cudaToNumpy(image_old)

			image_new = CudaImageBuffers[camera].get_historic_frame(0)
			
			numpy_new = jetson_utils.cudaToNumpy(image_new)
			
			delta = numpy.absolute(numpy.subtract(	numpy_old.astype(numpy.int16), 
													numpy_new.astype(numpy.int16)))

			movement = delta >= movement_delta_threshold  

			movement_hits = len(delta[movement])

			movement_hits_percent = float(movement_hits / (resized_image_bw[camera].width * resized_image_bw[camera].height)) * 100

			basic_stats[camera].update(movement_hits_percent)

			if movement_hits_percent >  movement_hits_threshold_percent:
				return movement_hits_percent
	
	return False

def send_smtp_message(camera, eventclass, image):

	global smtp_down
	
	if SmtpTimeoutCheck.expired():

		if not smtp_down:

			subject = camera
			body = eventclass
			message = MIMEMultipart()
			message["From"] = sender_email
			message["To"] = receiver_email
			message["Subject"] = subject
		
			message.attach(MIMEText(body, "plain"))
		
			filename = image
			with open(filename, "rb") as attachment:
				part = MIMEBase("application", "octet-stream")
				part.set_payload(attachment.read())

			encoders.encode_base64(part)

			part.add_header(
				"Content-Disposition",
				f"attachment; filename= {filename}",
			)

			message.attach(part)
			text = message.as_string()

			context = ssl.create_default_context()
			with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
				try:
					server.login(sender_email, smtp_password)
					server.sendmail(sender_email, receiver_email, text)
				except:
					smtp_down = True

def test_event_needs_alarmed(camera, confidence, eventclass): 
	
	retval = True
	try:
		configured_confidence_threshold = float(label_alarmlist_confidence_override[eventclass])
		if confidence >= float(configured_confidence_threshold):
			retval = True
		else:
			retval = False
	except:
		pass

	return retval

def GetBestDetection(camera, detections, detection_image_size):

	best_unfiltered_detection = None

	for detection in detections:

		detected_class	= int(detection.ClassID)
		eventclass		= label_name_index_array[detected_class]
		confidence		= round(detection.Confidence, 2)
		left			= int(detection.Left)
		right			= int(detection.Right)
		top				= int(detection.Top)		
		bottom			= int(detection.Bottom)

		percent_screenfill = float((((right - left) * (bottom - top)) / detection_image_size) * 100)	

		needs_filtered_reason  = test_event_needs_filtered(camera, eventclass, left, right, top, bottom, percent_screenfill)

		if needs_filtered_reason:
			logger.debug("Filtered out event reason '%s', %s-%s : %.2f %d,%d,%d,%d", 
								needs_filtered_reason, camera, eventclass, confidence, left, right, top, bottom)
			continue

		if not best_unfiltered_detection:
			best_unfiltered_detection = detection

		if test_event_needs_alarmed(camera, confidence, eventclass) == True:
			# a detection event needs alarmed so take that one without looking any further
			best_unfiltered_detection = detection
			return best_unfiltered_detection

		if not best_unfiltered_detection:
			best_unfiltered_detection = detection
		else:
			if confidence > round(best_unfiltered_detection.Confidence, 2):
				best_unfiltered_detection = detection

	return best_unfiltered_detection

def check_cameras_are_ok_on_startup():
	for camera, jetson_videoSource in rtsp_streams.items():
		try:
			height = jetson_videoSource.GetHeight()
			if not height:
				logger.error("Camera %s is not up, removing.", camera)
				rtsp_streams[camera] = None
			else:
				logger.info("Camera %s is up and running", camera)
		except:
			pass

	number_of_cameras_up = 0
	for camera in rtsp_streams:
		if rtsp_streams[camera] is not None:
			number_of_cameras_up = number_of_cameras_up + 1
	if number_of_cameras_up == 0:
		logger.critical("No cameras are operational, wIll shut down.")
		raise SystemExit
	return

# Initialisation

if len(sys.argv) != 2:
	print("I need config file location as first paramater in command line")
	sys.exit(1)

read_config(sys.argv[1]) 

if logging_level_debug > 0:
	logging_level = logging.DEBUG
else:
	logging_level = logging.INFO

logging.basicConfig(    handlers=[
								logging.FileHandler(logfile),
								logging.StreamHandler()],
						format='%(asctime)s,%(msecs)03d %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s',
						datefmt='%Y-%m-%d:%H:%M:%S',
						level=logging_level)

logger = logging.getLogger(__name__)

logger.info("CudaCam started")

for camera_details, uri in cameras.items():
	friendly_name, camera_type = camera_details.split(',')
	camera_ip_address[camera_details] = uri.split(':')[1][2:]
	if using_rtsp_simple_proxy:		
		cameras[camera_details]="rtsp://127.0.0.1:8554/" + friendly_name
		logger.info("Remapped %s to %s as using_rtsp_simple_proxy set", uri, cameras[camera_details])
	basic_stats[friendly_name] = BasicStatsAgainstThreshold(movement_hits_threshold_percent)
	input_codec_string = "----input-codec=" + camera_type
	rtsp_streams[friendly_name] = jetson_utils.videoSource(uri, ['me', input_codec_string])

logger.info("Starting inference engine, can take a while")
net = jetson_inference.detectNet("ssd-mobilenet-v2", threshold=baseline_model_confidence)
logger.info("Inference engine is up")

sqlite_connection = sqlite3.connect(sqlite_db)
sqlite_cursor = sqlite_connection.cursor() 

create_sqlite_event_table = '''CREATE TABLE IF NOT EXISTS events (
									camera text NOT NULL,
									timestamp text NOT NULL,
									eventclass text NOT NULL,
									confidence real NOT NULL,
									alarmed integer NOT NULL,
									left integer NOT NULL,
									right integer NOT NULL,
									top integer NOT NULL,
									bottom integer NOT NULL,
									image_filename text NOT NULL
								)'''

try:
	sqlite_cursor.execute(create_sqlite_event_table)
except Exception as e:
	logging.error('Exception occured: %s' % e, exc_info=True)
	pass

if config.has_section("mqtt"):
	mqtt_client = mqtt.Client()
	mqtt_client.username_pw_set(mqtt_username, mqtt_password)
	try:
		mqtt_client.connect(mqtt_ip_address, 1883)
	except:
		logger.error("Cannot connect to your MQTT server")
	mqtt_client.loop_start() 

StatsTimeoutCheck = TimeoutCheck(stats_update_seconds)
StatsTimeoutCheck.start()

SmtpTimeoutCheck = TimeoutCheck(smtp_update_seconds)
SmtpTimeoutCheck.start()

CameraRestartTimeoutCheck = TimeoutCheck(camera_attempt_restart_timer)
CameraRestartTimeoutCheck.start()

logger.info("Starting cameras and getting test images for %s, can take a while", mutelist_reminder_folder)

for name, jetson_videoSource in rtsp_streams.items(): 
	try:
		image = jetson_videoSource.Capture(format='rgb8', timeout = camera_starting_up_timeout*1000)


		image_ai[name] = jetson_utils.cudaAllocMapped(	width = image.width * ai_resize_factor,
														height = image.height * ai_resize_factor,
														format = image.format)

		image_bw[name] = jetson_utils.cudaAllocMapped(	width = image_ai[name].width,
														height = image_ai[name].height, 
														format="gray8")	

		resized_image_bw[name] = jetson_utils.cudaAllocMapped(	width = image_bw[name].width * motion_resize_factor,
																height = image_bw[name].height * motion_resize_factor,
																format="gray8")
		
		CudaImageBuffers[name] = FrameBuffer(	frame_check_delta + 1, 
												resized_image_bw[name].width, 
												resized_image_bw[name].height, 
												"gray8")

		jetson_utils.cudaResize(image, image_ai[name])

		try:
			filename = mutelist_reminder_folder + "/" + name + ".jpg"

			jetson_utils.saveImageRGBA(filename , image_ai[name], 
											image_ai[name].width, image_ai[name].height)

			source_image = Image.open(filename)
			draw = ImageDraw.Draw(source_image)

			try:
				mutelist_inside = object_mutelist_inside[name]
				for mutelist_name, mutelist in mutelist_inside.items():
					left,right,top,bottom = mutelist.split(',')
					draw.rectangle(((int(left), int(top)), (int(right), int(bottom))), width=3, outline="green")
					draw.text(((int(left)+int(right))/2, (int(top)+int(bottom))/2), mutelist_name, fill="green")
			except:
				pass

			try:
				mutelist_outside = object_mutelist_outside[name]
				for mutelist_name, mutelist in mutelist_outside.items():
					left,right,top,bottom = mutelist.split(',')
					draw.rectangle(((int(left), int(top)), (int(right), int(bottom))), width=3, outline="red")
					draw.text(((int(left)+int(right))/2, (int(top)+int(bottom))/2), mutelist_name, fill="red")
			except:
				pass

			source_image.save(filename, "JPEG")
		except:
			pass

	except:
		pass

check_cameras_are_ok_on_startup()

for camera, jetson_videoSource in rtsp_streams.items():
	basic_stats[camera].reset()	

# Main loop

while True:
	if StatsTimeoutCheck.expired():
		for camera, jetson_videoSource in rtsp_streams.items():
			if jetson_videoSource:
				minimum,maximum,average,count_events_exceeding_threshold,count,threshold = basic_stats[camera].getstats()
				if count != 0:		
					logger.info("Processing images for %s at %d fps, %d percent exceeding motion threshold.", 
								camera, round(count / stats_update_seconds), round((count_events_exceeding_threshold / count) * 100))
					basic_stats[camera].reset()			
		
	if CameraRestartTimeoutCheck.expired():
		for camera, jetson_videoSource in rtsp_streams.items():
			if not jetson_videoSource: 	
				### Find URI for that partucular camera and restart it if netowork layer is up
				for camera_details, uri in cameras.items():
					if camera in camera_details:

						hostname = camera_ip_address[camera_details]

						logger.info("Pinging %s to see if %s is now available for attempted restart", hostname, camera)

						response = os.system("ping -c 1 " + hostname)

						if 0 == 0: # response == 0:

							friendly_name, camera_type = camera_details.split(',')
							input_codec_string = "----input-codec=" + camera_type
							rtsp_streams[friendly_name] = jetson_utils.videoSource(uri, ['me', input_codec_string])
							jetson_videoSource = rtsp_streams[friendly_name]

							try:
								image = jetson_videoSource.Capture(format='rgb8', timeout = camera_starting_up_timeout * 1000)
								try:
									height = jetson_videoSource.GetHeight()
									if not height:
										logger.error("Camera %s is not up, removing.", camera)
										rtsp_streams[camera] = None
									else:
										logger.info("Camera %s is up and running", camera)
								except:
									logger.info("Camera %s is still down", camera)
									rtsp_streams[friendly_name] = None
							except:
								logger.info("Camera %s is still down (no image)", camera)
								rtsp_streams[friendly_name] = None
							
						else:
							logger.info("Camera %s is still down (no ping)", camera)
							rtsp_streams[friendly_name] = None

						break

	for camera, jetson_videoSource in rtsp_streams.items():
		if jetson_videoSource: 
			try:
				image = jetson_videoSource.Capture(format='rgb8', timeout = camera_down_timeout_ms)
			except:
				image = None
				continue
			if not image:
				logger.info("Timeout in getting image from %s", camera)
				logger.error("Camera %s is not up, removing.", camera)
				rtsp_streams[camera] = None

			if image:

				movement = False

				if ai_resize_factor != 1.0:
					jetson_utils.cudaResize(image, image_ai[camera])
				else:
					image_ai[camera] = image

				if movement_hits_threshold_percent > 0.0:
					movement = is_motion_detected(camera, image_ai[camera])
				else:
					basic_stats[camera].update(100.0)
					movement = True

				if movement:

					detections = net.Detect(image_ai[camera], 
								image_ai[camera].width, image_ai[camera].height, 'labels')	
						
					best_unfiltered_detection =	GetBestDetection(camera, detections, image_ai[camera].size)
					
					if best_unfiltered_detection:	

						detected_class	= int(best_unfiltered_detection.ClassID)
						eventclass		= label_name_index_array[detected_class]
						confidence		= round(best_unfiltered_detection.Confidence, 2)
						left			= int(best_unfiltered_detection.Left)
						right			= int(best_unfiltered_detection.Right)
						top				= int(best_unfiltered_detection.Top)		
						bottom			= int(best_unfiltered_detection.Bottom)
			
						timestamp = datetime.now().strftime("%d-%m-%Y-%H-%M-%S-%f")
						image_path = image_storage_dir + camera + "/"				
						image_filename	= timestamp + ".jpg"	
				
						new_image_filename = image_filename.split('-', 3)[-1] 
						split = image_filename.split('-', 3)
						image_filename_before_date  = split[0] + "-" + split[1] + "-" + split[2] 
						new_image_path =  image_path +  image_filename_before_date + "/"
						image_location = new_image_path + new_image_filename
						x = round((left + (right - left)/2)/image_ai[camera].width,2)
						y = round((image_ai[camera].height - (top + (bottom - top)/2))/image_ai[camera].height,2)
						
						needs_alarmed = test_event_needs_alarmed(camera, confidence, eventclass)
					
						sqlite_cursor.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)" , 
								(camera, timestamp, eventclass , confidence, needs_alarmed,  
									left, right, top, bottom, image_location))

						sqlite_connection.commit()

						if needs_alarmed:

							os.makedirs(os.path.dirname(new_image_path), exist_ok=True)
							
							jetson_utils.saveImageRGBA(image_location, image_ai[camera], image_ai[camera].width, image_ai[camera].height)
							
							message_needs_alarmed = camera + ":" + eventclass + ":" + str(x) + " " + str(y)
							if config.has_section("mqtt"):
								mqtt_client.publish(mqtt_topic, message_needs_alarmed) 

							if config.has_section("smtp"):
								send_smtp_message(camera, eventclass, image_location)

							logger.info("Event alarmed %s - %s : confidence %.2f : x,y : %.2f,%.2f", 
											camera, eventclass, confidence, x, y)
						else:
							logger.debug("Event not alarmed %s - %s : confidence %.2f : x,y : %.2f,%.2f", 
											camera, eventclass, confidence, x, y)
