#!/usr/bin/env python
# -*- coding: utf-8 -*-

import datetime
import io
import logging
import math
import os
import sys
import time
from typing import Dict, Union
import tensorflow as tf
from glob import glob
import argparse

import numpy as np
import pickle

from dataset import (
    BatchedSamples,
    CharToIndexType,
    ParalelSentencesDataset,
    read_dataset_files
)
from common import utils
from config import Config, BiLSTMConfig
from models.bilstm import BiLSTM


def parse_args():
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        "dataset", default='', type=str,
        help="Path to dataset configuration file storing files for train, dev and test."
    )
    parser.add_argument(
        "--config", required=True, type=str,
        help="Path to configuration file."
    )
    parser.add_argument(
        "--exp_name", default='', type=str,
        help="Experiment name."
    )
    parser.add_argument(
        "--input_char_vocab", default=None, type=str,
        help="Path to file storing input char vocabulary. If no provided, is automatically computed from data."
    )
    parser.add_argument(
        "--target_char_vocab", default=None, type=str,
        help="Path to file storing target char vocabulary. If no provided, is automatically computed from data."
    )
    parser.add_argument(
        "--savedir", default="../experiments", type=str,
        help="Savedir name."
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Runs the training steps eagerly, allowing for easier debugging."
    )

    args = parser.parse_args()
    return args


def load_vocabularies(
    input_vocab_path: Union[str, None],
    target_vocab_path: Union[str, None],
    checkpoint_path: Union[str, None]
):
    input_char_vocab: Union[CharToIndexType, None] = None
    target_char_vocab: Union[CharToIndexType, None] = None

    if input_vocab_path is not None and os.path.exists(input_vocab_path):
        input_char_vocab = utils.load_vocabulary(input_vocab_path)
    if target_vocab_path is not None and os.path.exists(target_vocab_path):
        target_char_vocab = utils.load_vocabulary(target_vocab_path)

    if checkpoint_path is not None:
        checkpoint_paths = glob(checkpoint_path)  # expand possible wildcard

        if len(checkpoint_paths) == 0:
            raise ValueError(
                f'Restore parameter provided ({checkpoint_path}), '
                'but no such folder exists.'
            )
        elif len(checkpoint_paths) > 1:
            raise ValueError(
                f'Restore parameter provided ({checkpoint_path}), '
                'but multiple such folders exist.'
            )

        checkpoint_path = checkpoint_paths[0]

        with open(os.path.join(checkpoint_path, 'vocab.pkl'), 'rb') as f:
            input_char_vocab, target_char_vocab = pickle.load(f)

    return input_char_vocab, target_char_vocab


def setup_session(args: argparse.Namespace):
    # Set random seed
    np.random.seed(42)

    config = Config(args.config, BiLSTMConfig)

    experiment_name = args.exp_name
    experiment_name += '_layers{}_dim{}_embedding{}_lr{}'.format(
        config.model_config.rnn_n_layers,
        config.model_config.rnn_cell_dim,
        config.model_config.char_embedding_dim,
        config.learning_config.optimizer_config.learning_rate
    )

    # create save directory for current experiment's data (if not exists)
    save_data_dir = os.path.join(args.savedir, experiment_name)
    if not os.path.exists(save_data_dir):
        os.makedirs(save_data_dir)

    # create subdir of save data directory to store trained models
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    save_model_dir = os.path.join(save_data_dir, timestamp)
    os.makedirs(save_model_dir)

    # update paths from running config
    chk_path = config.learning_config.running_config.checkpoint['filepath']
    chk_path = os.path.join(save_model_dir, chk_path)
    os.makedirs(os.path.dirname(chk_path))
    config.learning_config.running_config.checkpoint['filepath'] = chk_path

    sts_path = config.learning_config.running_config.states_dir
    sts_path = os.path.join(save_model_dir, sts_path)
    os.makedirs(sts_path)
    config.learning_config.running_config.states_dir = sts_path

    tns_path = config.learning_config.running_config.tensorboard['log_dir']
    tns_path = os.path.join(save_model_dir, tns_path)
    os.makedirs(tns_path)
    config.learning_config.running_config.tensorboard['log_dir'] = tns_path

    # configure logger
    logging.basicConfig(
        filename=os.path.join(save_model_dir, 'experiment_log.log'),
        level=logging.DEBUG,
        format='%(asctime)s %(message)s'
    )
    logging.info('Experiment started at: {} and its name: {}'.format(
        timestamp, experiment_name
    ))
    logging.info('Experiment arguments: {}'.format(str(args)))

    return save_model_dir, config


def add_dev_and_test_sets(
    dataset: ParalelSentencesDataset,
    ds_fpaths: Dict[str, str]
):
    if 'dev_inputs' in ds_fpaths:
        print('Loading validation data')
        dev_input_sentences, dev_target_sentences = read_dataset_files(
            inputs_filepath=ds_fpaths['dev_inputs'],
            targets_filepath=ds_fpaths['dev_targets'],
            sentence_limit=None
        )
        dataset.add_validation_set(dev_input_sentences, dev_target_sentences)

    if 'test_inputs' in ds_fpaths:
        print('Loading test data')
        test_input_sentences, test_target_sentences = read_dataset_files(
            inputs_filepath=ds_fpaths['dev_inputs'],
            targets_filepath=ds_fpaths['dev_targets'],
            sentence_limit=None
        )
        dataset.add_test_set(test_input_sentences, test_target_sentences)


def main(args: argparse.Namespace):
    save_model_dir, config = setup_session(args)

    ds_fpaths = utils.parse_dataset_file(args.dataset)
    print(ds_fpaths, '\nLoading train data')

    input_sentences, target_sentences = read_dataset_files(
        inputs_filepath=ds_fpaths['train_inputs'],
        targets_filepath=ds_fpaths['train_targets'],
        sentence_limit=config.learning_config.dataset_config.sentence_limit
    )

    input_char_vocab, target_char_vocab = load_vocabularies(
        input_vocab_path=args.input_char_vocab,
        target_vocab_path=args.target_char_vocab,
        checkpoint_path=None
    )

    batch_size = config.learning_config.running_config.batch_size

    dataset = ParalelSentencesDataset(
        batch_size=batch_size,
        max_chars_in_sentence=config.learning_config.dataset_config.max_chars_in_sentence,
        input_sentences=input_sentences,
        target_sentences=target_sentences,
        input_char_vocabulary=input_char_vocab,
        target_char_vocabulary=target_char_vocab,
        take_num_top_chars=config.learning_config.dataset_config.take_num_top_chars
    )
    add_dev_and_test_sets(dataset, ds_fpaths)

    print('Building dataset')
    dataset.build()

    input_char_vocab = dataset.input_char_vocabulary
    assert input_char_vocab is not None
    target_char_vocab = dataset.target_char_vocabulary
    assert target_char_vocab is not None

    # dump current configuration and used vocabulary to this model's folder
    with open(os.path.join(save_model_dir, 'vocab.pkl'), 'wb') as f:
        pickle.dump((input_char_vocab, target_char_vocab), f)
    with open(os.path.join(save_model_dir, 'config.pkl'), 'wb') as f:
        pickle.dump(args, f)

    model = BiLSTM(
        lstm_units=config.model_config.rnn_cell_dim,
        num_rnns=config.model_config.rnn_n_layers,
        input_alphabet_size=len(input_char_vocab.keys()),
        target_alphabet_size=len(target_char_vocab.keys()),
        embedding_dim=config.model_config.char_embedding_dim,
        use_residual=config.model_config.use_residual,
        dropout=config.model_config.dropout
    )

    model.make(batch_size)
    model.summary(line_length=80)

    optimizer = tf.keras.optimizers.Adam(
        **config.learning_config.optimizer_config.__dict__
    )

    model.compile(
        optimizer=optimizer,
        run_eagerly=args.debug,
    )

    train_loader, dev_loader = dataset.get_loaders(batch_size=batch_size)

    callbacks = [
        tf.keras.callbacks.ModelCheckpoint(
            **config.learning_config.running_config.checkpoint
        ),
        tf.keras.callbacks.experimental.BackupAndRestore(
            config.learning_config.running_config.states_dir
        ),
        tf.keras.callbacks.TensorBoard(
            **config.learning_config.running_config.tensorboard
        ),
    ]

    model.fit(
        train_loader,
        validation_data=dev_loader,
        batch_size=batch_size,
        epochs=config.learning_config.running_config.num_epochs,
        callbacks=callbacks
    )


if __name__ == "__main__":
    main(parse_args())