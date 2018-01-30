import os
import numpy as np
from tqdm import trange
import tensorflow as tf
from tensorflow.contrib.framework.python.ops import arg_scope
import scipy.stats as st
import glob
from scipy import misc
from PIL import Image
from skimage import color
from scipy.misc import imresize
from layers import normalize
import sys
import glob
import random

from model import Model
from buffer import Buffer
import data.nuclei_data as nuclei_data
from utils import imwrite, imread, img_tile, synthetic_to_refer_paths

class Trainer(object):
  def __init__(self, config, rng):
    self.config = config
    self.rng = rng

    self.model_dir = config.model_dir
    self.gpu_memory_fraction = config.gpu_memory_fraction

    self.log_step = config.log_step
    self.max_step_d_g = config.max_step_d_g
    self.max_step_d_g_l = config.max_step_d_g_l

    self.PS = config.input_width

    self.load_path = config.load_path
    self.K_d = config.K_d
    self.K_g = config.K_g
    self.K_l = config.K_l
    self.initial_K_d = config.initial_K_d
    self.initial_K_g = config.initial_K_g
    self.initial_K_l = config.initial_K_l
    self.after_K_l = config.after_K_l
    self.checkpoint_secs = config.checkpoint_secs

    DataLoader = {
        'nuclei': nuclei_data.DataLoader,
    }[config.data_set]
    self.data_loader = DataLoader(config, rng=self.rng)


    ps_hosts = config.ps_hosts.split(",")
    worker_hosts = config.worker_hosts.split(",")

    # Create a cluster from the parameter server and worker hosts.
    cluster = tf.train.ClusterSpec({"ps": ps_hosts, "worker": worker_hosts})

    # Create and start a server for the local task.
    server = tf.train.Server(cluster,
                             job_name=config.job_name,
                             task_index=config.task_index)

    if config.job_name == "ps":
      server.join()
    elif config.job_name == "worker":

      with tf.device(tf.train.replica_device_setter(
                     worker_device="/job:worker/replica:0/task:%d/gpu:%d" % (config.task_index, config.gpu),
                     cluster=cluster)):
      #with tf.device("/job:localhost/task:%d/gpu:%d" % (config.task_index, config.gpu)):

        self.model = Model(config, self.data_loader)
        self.history_buffer = Buffer(config, self.rng)

        self.summary_ops = {
                'test_synthetic_images': {
                'summary': tf.summary.image("test_synthetic_images",
                                            self.model.x,
                                            max_outputs=config.max_image_summary),
                'output': self.model.x,
            },
            'test_refined_images': {
                'summary': tf.summary.image("test_refined_images",
                                            self.model.denormalized_R_x,
                                            max_outputs=config.max_image_summary),
                'output': self.model.denormalized_R_x,
            },
            'test_refer_images': {
                'summary': tf.summary.image("test_refer_images",
                                            self.model.ref_image,
                                            max_outputs=config.max_image_summary),
                'output': self.model.ref_image,
            },
            'test_learner_outputs': {
                'summary': tf.summary.image("test_learner_outputs",
                                            self.model.L_R_x*255,
                                            max_outputs=config.max_image_summary),
                'output': self.model.L_R_x*255,
            },
        }

 
        self.saver = tf.train.Saver()
        self.summary_writer = tf.summary.FileWriter(self.model_dir)

        sv = tf.train.Supervisor(logdir=self.model_dir,
                                 is_chief=(config.task_index == 0),
                                 saver=self.saver,
                                 summary_op=None,
                                 summary_writer=self.summary_writer,
                                 save_summaries_secs=300,
                                 save_model_secs=self.checkpoint_secs,
                                 global_step=self.model.learner_step)

        gpu_options = tf.GPUOptions(
            per_process_gpu_memory_fraction=self.gpu_memory_fraction,
            allow_growth=True) # seems to be not working
        sess_config = tf.ConfigProto(allow_soft_placement=True,
                                     gpu_options=gpu_options,
                                     log_device_placement=True)

        #self.sess = sv.prepare_or_wait_for_session(config=sess_config)
        self.sess = sv.prepare_or_wait_for_session(server.target, config=sess_config)

  def train(self):
    print("[*] Training starts...")
    self._summary_writer = None

    sample_num = reduce(lambda x, y: x*y, self.config.sample_image_grid)
    idxs = self.rng.choice(len(self.data_loader.synthetic_data_paths), sample_num)
    synthetic_paths = self.data_loader.synthetic_data_paths[idxs];
    synthetic_ref_paths = synthetic_to_refer_paths(synthetic_paths, self.config);

    test_samples = np.stack([imread(path) for path in synthetic_paths]);
    test_refer_samples = np.stack([imread(path) for path in synthetic_ref_paths]);

    if test_samples.ndim == 3:
      test_samples = np.expand_dims(test_samples, -1)
    test_samples = test_samples[:, 0:self.config.input_height, 0:self.config.input_width, :];
    if test_refer_samples.ndim == 3:
      test_refer_samples = np.expand_dims(test_refer_samples, -1)
    test_refer_samples = test_refer_samples[:, 0:self.config.input_height, 0:self.config.input_width, :];

    def train_refiner(push_buffer=False):
      feed_dict = {
        self.model.synthetic_batch_size: self.data_loader.batch_size,
      }
      res = self.model.train_refiner(
          self.sess, feed_dict, self._summary_writer, with_output=True)
      self._summary_writer = self._get_summary_writer(res)

      if push_buffer:
        self.history_buffer.push(res['output'])

      if res['step'] % self.log_step == 0:
        feed_dict = {
            self.model.x: test_samples,
            self.model.ref_image: test_refer_samples,
        }
        self._inject_summary(
          'test_refined_images', feed_dict, res['step'])
        self._inject_summary(
          'test_learner_outputs', feed_dict, res['step'])

        if res['step'] / float(self.log_step) == 1.:
          self._inject_summary(
              'test_synthetic_images', feed_dict, res['step'])
          self._inject_summary(
              'test_refer_images', feed_dict, res['step'])

    def train_discrim():
      a, b, c = self.history_buffer.sample()
      d, e = self.data_loader.next()

      feed_dict = {
        self.model.synthetic_batch_size: self.data_loader.batch_size/2,
        self.model.R_x_history: a,
        self.model.refimg_history: c,
        self.model.y: d,
        self.model.ref_y: e,
      }
      res = self.model.train_discrim(
          self.sess, feed_dict, self._summary_writer, with_history=True, with_output=False)
      self._summary_writer = self._get_summary_writer(res)

    def train_learner():
      a, b, c = self.history_buffer.sample()

      feed_dict = {
        self.model.synthetic_batch_size: self.data_loader.batch_size/2,
        self.model.R_x_history: a,
        self.model.mask_history: b,
      }
      res = self.model.train_learner(
          self.sess, feed_dict, self._summary_writer, with_output=False)

      self._summary_writer = self._get_summary_writer(res)

    for k in trange(self.initial_K_g, desc="Train refiner"):
      train_refiner(push_buffer=(k>self.initial_K_g*0.9))

    for k in trange(self.initial_K_d, desc="Train discrim"):
      train_discrim()

    for step in trange(self.max_step_d_g, desc="Train refiner+discrim"):
      for k in xrange(self.K_g):
        train_refiner(push_buffer=True)

      for k in xrange(self.K_d):
        train_discrim()

    for k in trange(self.initial_K_l, desc="Train learner"):
      train_learner()

    for step in trange(self.max_step_d_g_l, desc="Train all Three"):
      for k in xrange(self.K_g):
        train_refiner(push_buffer=True)
      for k in xrange(self.K_l):
        train_learner()
      for k in xrange(self.K_d):
        train_discrim()

    for k in trange(self.after_K_l, desc="Train learner"):
      train_learner()

  def test(self):
    self.cnn_pred_mask('./segmentation_test_images/')

  def gkern(self, kernlen=21, nsig=3):
    """Returns a 2D Gaussian kernel array."""
    interval = (2*nsig+1.)/(kernlen)
    x = np.linspace(-nsig-interval/2., nsig+interval/2., kernlen+1)
    kern1d = np.diff(st.norm.cdf(x))
    kernel_raw = np.sqrt(np.outer(kern1d, kern1d))
    kernel = kernel_raw/kernel_raw.sum()
    return kernel;

  def load_data(self, image_folder):
    X = [];
    F = [];
    R = [];

    tile_list = image_folder + '/image_resize_list.txt';
    lines = [line.strip() for line in open(tile_list, 'r')];
    for line in lines:
      imag_path = image_folder + '/' + line.split()[0];
      resize_f = float(line.split()[1]);
      if abs(resize_f - 1) >= 0.001:
        imag = misc.imresize(np.array(Image.open(imag_path).convert('RGB')), resize_f).astype(np.float32);
      else:
        imag = np.array(Image.open(imag_path).convert('RGB')).astype(np.float32);
      X.append(normalize(imag));
      F.append(imag_path.split('/')[-1]);
      R.append(resize_f);

    print 'Tiles loaded.', len(X);
    return X, F, R;

  def cnn_pred_mask(self, segmentation_folder):
    PS = self.PS;
    step_size = self.config.pred_step_size;
    gsm = self.gkern(PS, self.config.pred_gkern_sig);
    X, F, R = self.load_data(segmentation_folder);
    print 'Scaling factor {}'.format(self.config.pred_scaling);

    for im_id in range(len(X)):
      print 'Segmenting {}'.format(F[im_id]);

      img = X[im_id];
      outf = segmentation_folder + '/' + F[im_id][:-4] + "_pred.png";
      resize_f = R[im_id] * self.config.pred_scaling;

      pred_m = np.zeros((img.shape[0], img.shape[1]), dtype=np.float32);
      num_m = np.zeros((img.shape[0], img.shape[1]), dtype=np.float32);

      for mir in range(2):
        img = img[::-1, :, :]
        pred_m = pred_m[::-1, :];
        num_m = num_m[::-1, :];
        for rot in range(4):
          img = np.swapaxes(img, 0, 1)[::-1, :, :];
          pred_m = np.swapaxes(pred_m, 0, 1)[::-1, :];
          num_m = np.swapaxes(num_m, 0, 1)[::-1, :];
          for x in range(0, pred_m.shape[0]-PS+1, step_size) + [pred_m.shape[0]-PS]:
            nimg = len(range(0, pred_m.shape[1]-PS+1, step_size) + [pred_m.shape[1]-PS]);
            net_inputs = np.zeros(shape=(nimg, img.shape[2], PS, PS), dtype=np.float32);
            yind = 0;
            for y in range(0, pred_m.shape[1]-PS+1, step_size) + [pred_m.shape[1]-PS]:
              net_inputs[yind, :, :, :] = img[x:x+PS, y:y+PS, :].transpose();
              yind += 1;

            feed_dict = {
              self.model.test_patch_normalized: np.transpose(net_inputs, [0,2,3,1]),
            }

            res_discrim = self.model.test_learner_patch(self.sess, feed_dict, None, with_output=True)

            net_outputs = res_discrim['output']
            net_outputs = np.squeeze(net_outputs, axis=3);
            yind = 0;
            for y in range(0, pred_m.shape[1]-PS+1, step_size) + [pred_m.shape[1]-PS]:
              pred_m[x:x+PS, y:y+PS] += net_outputs[yind, :, :].transpose() * gsm;
              num_m[x:x+PS, y:y+PS] += gsm;
              yind += 1;

      pred_m /= num_m;
      if abs(resize_f - 1) >= 0.001:
        pred_m = misc.imresize(pred_m, 1.0/resize_f);
      imwrite(outf, pred_m);

  def _inject_summary(self, tag, feed_dict, step):
    summaries = self.sess.run(self.summary_ops[tag], feed_dict)
    self.summary_writer.add_summary(summaries['summary'], step)

    path = os.path.join(self.config.sample_model_dir, "{}_{}.png".format(tag, step))
    tile = img_tile(summaries['output'], tile_shape=self.config.sample_image_grid);
    if tile.shape[2] == 1:
        tile = tile[:, :, 0];
    imwrite(path, tile);

  def _get_summary_writer(self, result):
    if result['step'] % self.log_step == 0:
      return self.summary_writer
    else:
      return None
