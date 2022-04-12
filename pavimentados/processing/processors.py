import tensorflow as tf
from pathlib import Path
import json
import os
import cv2
import numpy as np
from pavimentados.models.structures import Yolo_Model, Siamese_Model, State_Signal_Model
from pavimentados.image.utils import transform_images
from pavimentados.configs.utils import Config_Basic
from tqdm import tqdm

pavimentados_path = Path(__file__).parent.parent

class Image_Processor(Config_Basic):
	def __init__(self, yolo_device = '/device:CPU:0', siamese_device = '/device:CPU:0', state_device = '/device:CPU:0', config_file = pavimentados_path / 'configs' / 'processor.json'):
		self.yolo_device = yolo_device
		self.siamese_device = siamese_device
		self.state_device = state_device
		self.load_config(config_file)
		self.load_models()

	def load_models(self):
		self.yolo_model = Yolo_Model(device = self.yolo_device)
		self.siamese_model = Siamese_Model(device = self.siamese_device)
		self.state_signal_model = State_Signal_Model(device = self.state_device)
	
	def get_yolo_output(self, images):
		return self.yolo_model.model.predict(images)
	
	def select_detections(self, predictions, selection_type = 'paviment'):
		boxes, scores, classes, numbers = predictions
		thresholds = [np.array([self.config['thressholds'][selection_type].get(elem,0.2) for elem in classes[j]]) for j in range(len(classes))]
		classes = [classes[j][scores[j]>thresholds[j]].tolist() for j in range(len(classes))]
		boxes = [boxes[j][scores[j]>thresholds[j]].tolist() for j in range(len(boxes))]
		scores = [scores[j][scores[j]>thresholds[j]].tolist() for j in range(len(scores))]
		return boxes, scores, classes
	
	def crop_img(self, box, img):
		img_crop = img[int(box[1]*img.shape[0]):int(box[3]*img.shape[0]),int(box[0]*img.shape[1]):int(box[2]*img.shape[1])]
		img_crop = cv2.resize(img_crop, tuple(self.siamese_model.config['SIAMESE_IMAGE_SIZE'])[:2], interpolation = cv2.INTER_AREA).astype(float)/255
		return img_crop
	
	def predict_signal_state_single(self, image, box):
		if len(box)>0:
			crop_images = list(map(lambda x: self.crop_img(x, image), box))
			signal_pred_scores, pred_signal_base, pred_signal = self.siamese_model.predict(np.array(crop_images))
			pred_state = np.argmax(self.state_signal_model.predict(np.array(crop_images)), axis=1).tolist()
			return pred_signal, pred_signal_base, pred_state
		else:
			return [], [], []
		
	
	def predict_signal_state(self, images, boxes):
		mixed_results = map(lambda img, box: self.predict_signal_state_single(img, box), images, boxes)
		
		signal_predictions, signal_base_predictions, state_predictions = list(zip(*mixed_results))
		return list(signal_predictions), list(signal_base_predictions), list(state_predictions)


class Group_Processor(Config_Basic):
	def __init__(self, processor_config_file = pavimentados_path / 'configs' / 'processor.json', assign_devices = False, gpu_enabled = False, total_mem = 6144, 
				 yolo_device = '/device:CPU:0', siamese_device = '/device:CPU:0', state_device = '/device:CPU:0'):
			
		self.assign_model_devices(assign_devices, gpu_enabled, total_mem, yolo_device, siamese_device, state_device)
		self.processor = Image_Processor(yolo_device = self.yolo_device, siamese_device = self.siamese_device, 
										 state_device = self.state_device, config_file = processor_config_file)
	
	def assign_model_devices(self, assign_devices, gpu_enabled, total_mem, yolo_device, siamese_device, state_device):
		if assign_devices == True:
			if gpu_enabled == True:
				self.assign_gpu_devices(total_mem)
			else:
				self.yolo_device = '/device:CPU:0'
				self.siamese_device = '/device:CPU:0'
				self.state_device = '/device:CPU:0'
		else:
			self.yolo_device = yolo_device
			self.siamese_device = siamese_device
			self.state_device = state_device
			
	def assign_gpu_devices(self, total_mem):
		memory_unit = int(total_mem/6)
		gpus = tf.config.experimental.list_physical_devices('GPU')
		if gpus:   
			try:	
				tf.config.experimental.set_virtual_device_configuration(gpus[0], 
																		[tf.config.experimental.VirtualDeviceConfiguration(memory_limit=4*memory_unit),		 
																		 tf.config.experimental.VirtualDeviceConfiguration(memory_limit=memory_unit),
																		 tf.config.experimental.VirtualDeviceConfiguration(memory_limit=memory_unit)])	
				logical_gpus = tf.config.experimental.list_logical_devices('GPU')	
				print(len(gpus), "Physical GPU,", len(logical_gpus), "Logical GPUs")  
			except RuntimeError as e:
				print(e)
		self.yolo_device = logical_gpus[0].name
		self.siamese_device = logical_gpus[1].name
		self.state_device = logical_gpus[2].name	 


class MultiImage_Processor(Group_Processor):
	def __init__(self, config_file = pavimentados_path / 'configs' / 'images_processor.json' , processor_config_file = pavimentados_path / 'configs' / 'processor.json', assign_devices = False, gpu_enabled = False, total_mem = 6144, 
				 yolo_device = '/device:CPU:0', siamese_device = '/device:CPU:0', state_device = '/device:CPU:0'):
		super().__init__(processor_config_file = processor_config_file, assign_devices = assign_devices, gpu_enabled = gpu_enabled, total_mem = total_mem, 
				 yolo_device = yolo_device, siamese_device = siamese_device, state_device = state_device)
		self.load_config(config_file)
	
	def _process_batch(self, img_batch):
		transformed_batch = tf.convert_to_tensor([transform_images(img, 416) for img in img_batch]).numpy()
		prediction = self.processor.get_yolo_output(transformed_batch)
		boxes_pav, scores_pav, classes_pav = self.processor.select_detections(prediction[0], 'paviment')
		boxes_signal, scores_signal, classes_signal = self.processor.select_detections(prediction[1], 'signals')
		final_signal_classes, signal_base_predictions, state_predictions = self.processor.predict_signal_state(img_batch, boxes_signal)
		return (list(boxes_pav), list(boxes_signal), list(scores_pav), list(scores_signal),  
					list(classes_pav), list(classes_signal), final_signal_classes, 
					signal_base_predictions, state_predictions)
		
	def process_images_group(self, img_obj, batch_size = 8):#image_type = 'routes', batch_size = 8):
		len_imgs = img_obj.get_len()
		results = list(tqdm(map(lambda x: self._process_batch(img_obj.get_batch(x, batch_size)), [offset for offset in range(0,img_obj.get_len(), batch_size)]), total = int(len_imgs//batch_size)+int((len_imgs%batch_size)>0) ))
		results = list(zip(*results))
		return {
			'boxes_pav': sum(results[0], []), 
			'boxes_signal': sum(results[1], []), 
			'scores_pav': sum(results[2], []), 
			'scores_signal': sum(results[3], []),
			'classes_pav': sum(results[4], []), 
			'classes_signal': sum(results[5], []), 
			'final_pav_clases': [[self.processor.yolo_model.config['yolo_pav_dict_clases'].get(elem, '<UNK>')  for elem in item] for item in sum(results[4], [])], 
			'final_signal_classes': sum(results[6], []), 
			'signal_base_predictions': sum(results[7], []), 
			'state_predictions': sum(results[8], [])
		}		
	
	def process_folder(self, folder, batch_size = 8):
		folder = Path(folder)
		image_list = list(filter(lambda x: str(x).lower().split('.')[-1] in self.config['images_allowed'], map(lambda x: folder / x , os.listdir(folder))))
		return self.process_images_group(image_list, image_type = 'routes', batch_size = batch_size)