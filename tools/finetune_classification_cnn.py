"""
Finetunes a CNN for classification on a custom dataset using keras
Author: Jeff Mahler
"""
import argparse
import cPickle as pkl
import logging
import IPython
import numpy as np
import os
import random
import sys
import time

import scipy.misc as sm
import scipy.stats as ss

from keras import backend as K
from keras.callbacks import Callback, ModelCheckpoint
from keras.layers import Dense, Input, GlobalAveragePooling2D, Reshape
from keras.models import Model
from keras.preprocessing.image import ImageDataGenerator, Iterator, transform_matrix_offset_center, apply_transform
from keras.applications.imagenet_utils import _obtain_input_shape
from keras.optimizers import SGD
from keras.utils import to_categorical

import autolab_core.utils as utils
from autolab_core import YamlConfig
from perception import Image, RgbdImage
from perception.models.constants import *
from perception.models import ClassificationCNN, FinetunedClassificationCNN
from perception.models import TrainHistory, TensorDataGenerator, TensorDatasetIterator
from visualization import Visualizer2D as vis

from dexnet.learning import ClassificationResult
from dexnet.learning import TensorDataset, Tensor

def finetune_classification_cnn(config):
    """ Main function. """
    # read params
    dataset = config['dataset']
    x_names = config['x_names']
    y_name = config['y_name']
    model_dir = config['model_dir']
    debug = config['debug']

    batch_size = config['training']['batch_size']
    train_pct = config['training']['train_pct']
    model_save_period = config['training']['model_save_period']

    data_aug_config = config['data_augmentation']
    preproc_config = config['preprocessing']
    iterator_config = config['data_iteration']
    model_config = config['model']
    base_model_config = model_config['base']
    optimization_config = config['optimization']
    train_config = config['training']

    model_params = {}
    if 'params' in model_config.keys():
        model_params = model_config['params']
    
    base_model_params = {}
    if 'params' in base_model_config.keys():
        base_model_params = base_model_config['params']

    if debug:
        seed = 105
        random.seed(seed)
        np.random.seed(seed)

    # generate model dir
    if not os.path.exists(model_dir):
        os.mkdir(model_dir)
    model_id = utils.gen_experiment_id()
    model_dir = os.path.join(model_dir, 'model_%s' %(model_id))
    if not os.path.exists(model_dir):
        os.mkdir(model_dir)
    logging.info('Saving model to %s' %(model_dir))
    latest_model_filename = os.path.join(model_dir, 'weights_{epoch:05d}.h5')
    best_model_filename = os.path.join(model_dir, 'weights.h5')

    # save config
    training_config_filename = os.path.join(model_dir, 'training_config.yaml')
    config.save(training_config_filename)

    # open dataset
    dataset = TensorDataset.open(dataset)
    
    # split dataset
    indices_filename = os.path.join(model_dir, 'splits.npz')
    if os.path.exists(indices_filename):
        indices = np.load(indices_filename)['arr_0'].tolist()
        train_indices = indices['train']
        val_indices = indices['val']
    else:
        train_indices, val_indices = dataset.split(train_pct)
        indices = np.array({'train':train_indices,
                            'val':val_indices})
        np.savez_compressed(indices_filename, indices)
    num_train = train_indices.shape[0]
    num_val = val_indices.shape[0]
    val_steps = int(np.ceil(float(num_val) / batch_size))

    # init generator
    train_generator_filename = os.path.join(model_dir, 'train_preprocessor.pkl')
    val_generator_filename = os.path.join(model_dir, 'val_preprocessor.pkl')
    if os.path.exists(train_generator_filename):
        logging.info('Loading generators')
        train_generator = pkl.load(open(train_generator_filename, 'rb'))
        val_generator = pkl.load(open(val_generator_filename, 'rb'))
    else:
        logging.info('Fitting generator')
        train_generator = TensorDataGenerator(**data_aug_config)
        val_generator = TensorDataGenerator(featurewise_center=data_aug_config['featurewise_center'],
                                            featurewise_std_normalization=data_aug_config['featurewise_std_normalization'])
        fit_start = time.time()
        train_generator.fit(dataset, x_names, y_name, indices=train_indices, **preproc_config)
        val_generator.mean = train_generator.mean
        val_generator.std = train_generator.std
        val_generator.min_output = train_generator.min_output
        val_generator.max_output = train_generator.max_output
        fit_stop = time.time()
        logging.info('Generator fit took %.3f sec' %(fit_stop - fit_start))
        pkl.dump(train_generator, open(train_generator_filename, 'wb'))
        pkl.dump(val_generator, open(val_generator_filename, 'wb'))
    num_classes = int(train_generator.max_output + 1)

    # init iterator
    train_iterator = train_generator.flow_from_dataset(dataset, x_names, y_name,
                                                       indices=train_indices,
                                                       batch_size=batch_size,
                                                       **iterator_config)
    val_iterator = val_generator.flow_from_dataset(dataset, x_names, y_name,
                                                   indices=val_indices,
                                                   batch_size=batch_size,
                                                   **iterator_config)

    #batch_x, batch_y = train_iterator.next()
    
    """
    cnn = ClassificationCNN.open('test_model')
    batch_x, batch_y = val_iterator.next()
    IPython.embed()
    exit(0)
    """

    # setup model
    base_cnn = ClassificationCNN.open(base_model_config['model'],
                                      base_model_config['type'],
                                      input_name=x_names[0],
                                      **base_model_params)
    cnn = FinetunedClassificationCNN(base_cnn=base_cnn,
                                     name='dexresnet',
                                     num_classes=num_classes,
                                     output_name=y_name,
                                     im_preprocessor=val_generator,
                                     **model_params)

    # setup training
    cnn.freeze_base_cnn()
    optimizer = SGD(lr=optimization_config['lr'],
                    momentum=optimization_config['momentum'])
    model = cnn.model #Model(cnn.input_tensor, cnn.output_tensor, name='a')
    model.compile(optimizer=optimizer,
                  loss=optimization_config['loss'],
                  metrics=optimization_config['metrics'])
    w, b = model.layers[-1].get_weights()

    # sample batches
    num_test = 1
    x_batches = []
    y_batches = []
    for i in range(num_test):
        logging.info('Sampling batch %d' %(i))
        batch_x, batch_y = train_iterator.next()
        x_batches.append(batch_x)
        y_batches.append(batch_y)
    logging.info('Before')
    for i, batches in enumerate(zip(x_batches, y_batches)):
        logging.info('Scoring batch %d' %(i))
        batch_x, batch_y = batches
        pred_probs = model.predict(batch_x[x_names[0]])
        true_labels = np.argmax(batch_y, axis=1)
        c = ClassificationResult([pred_probs],[true_labels])
        score = model.evaluate(batch_x, batch_y)
        logging.info('Batch acc %.3f' %(c.accuracy))
        logging.info('Batch loss %.3f' %(score[0]))
        logging.info('Batch acc2 %.3f' %(score[1]))
        

    # train
    steps_per_epoch = int(np.ceil(float(num_train) / batch_size))
    latest_model_ckpt = ModelCheckpoint(latest_model_filename, period=model_save_period)
    best_model_ckpt = ModelCheckpoint(best_model_filename,
                                      save_best_only=True,
                                      period=model_save_period)
    train_history_cb = TrainHistory(model_dir)
    callbacks = [] #latest_model_ckpt, best_model_ckpt, train_history_cb]
    history = model.fit_generator(train_iterator,
                                  steps_per_epoch=steps_per_epoch,
                                  epochs=train_config['epochs'],
                                  #callbacks=callbacks,
                                  validation_data=val_iterator,
                                  validation_steps=val_steps,
                                  class_weight=train_config['class_weight'],
                                  use_multiprocessing=train_config['use_multiprocessing'])
    """
    model.fit(batch_x['rgbd_ims'], batch_y, 64,
              epochs=25, verbose=1, validation_data=(batch_x['rgbd_ims'], batch_y))
    """
    # test
    logging.info('After')
    for i, batches in enumerate(zip(x_batches, y_batches)):
        logging.info('Scoring batch %d' %(i))
        batch_x, batch_y = batches
        pred_probs = model.predict(batch_x[x_names[0]])
        true_labels = np.argmax(batch_y, axis=1)
        c = ClassificationResult([pred_probs],[true_labels])
        score = model.evaluate(batch_x, batch_y)
        logging.info('Batch acc %.3f' %(c.accuracy))
        logging.info('Batch loss %.3f' %(score[0]))
        logging.info('Batch acc2 %.3f' %(score[1]))
    IPython.embed()

    # save model
    cnn.save(model_dir)
    
    # save history
    history_filename = os.path.join(model_dir, 'history.pkl')
    pkl.dump(history.history, open(history_filename, 'wb'))

if __name__ == '__main__':
    # set logging
    logging.getLogger().setLevel(logging.INFO)

    # read args
    parser = argparse.ArgumentParser(description='Fine-tune a Classification CNN trained on ImageNet on a custom image dataset using TensorFlow')
    parser.add_argument('config_filename', type=str, default=None, help='path to the configuration file to use')
    args = parser.parse_args()
    config_filename = args.config_filename
    
    # read config
    config = YamlConfig(config_filename)

    # finetune
    finetune_classification_cnn(config)

