from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import json
import numpy as np
import random
import torch
import torchaudio
import torch.utils.data as tud

from speech.utils import io


class Preprocessor:
    END = "</s>"
    START = "<s>"

    def __init__(self, data_json, audio_cfg, max_samples=100, start_and_end=True):
        """
        Builds a preprocessor from a dataset.
        Arguments:
            data_json (string): A file containing a json representation
                of each example per line.
            max_samples (int): The maximum number of examples to be used
                in computing summary statistics.
            start_and_end (bool): Include start and end tokens in labels.
        """
        data = read_data_json(data_json)
        self.audio_cfg = audio_cfg

        # Compute data mean, std from sample
        audio_files = [d['audio'] for d in data]
        random.shuffle(audio_files)
        self.mean, self.std = compute_mean_std(self.audio_cfg, audio_files[:max_samples])
        self._input_dim = self.mean.shape[0]

        # Make char map
        chars = list(set(t for d in data for t in d['text']))
        if start_and_end:
            # START must be last so it can easily be
            # excluded in the output classes of a model.
            chars.extend([self.END, self.START])
        self.start_and_end = start_and_end
        self.int_to_char = dict(enumerate(chars))
        self.char_to_int = {v: k for k, v in self.int_to_char.items()}

    def encode(self, text):
        text = list(text)
        if self.start_and_end:
            text = [self.START] + text + [self.END]
        return [self.char_to_int[t] for t in text]

    def decode(self, seq):
        text = [self.int_to_char[s] for s in seq]
        if not self.start_and_end:
            return text

        s = text[0] == self.START
        e = len(text)
        if text[-1] == self.END:
            e = text.index(self.END)
        return text[s:e]

    def preprocess(self, audio_cfg, pth=None, audio=None, text=()):
        if pth is not None:
            inputs = log_specgram_from_file(audio_cfg, pth)
        elif audio is not None:
            inputs = log_specgram(audio_cfg, audio)
        else:
            raise Exception("Either audio or pth should be None")
        inputs = (inputs - self.mean[: ,None]) / self.std[:, None]
        targets = self.encode(text)
        return inputs.T, targets

    @property
    def input_dim(self):
        return self._input_dim

    @property
    def vocab_size(self):
        return len(self.int_to_char)


def compute_mean_std(cfg, audio_files):
    samples = [log_specgram_from_file(cfg, af) for af in audio_files]
    samples = torch.vstack(samples)
    mean = torch.mean(samples, dim=0)
    std = torch.std(samples, dim=0)
    return mean, std


class AudioDataset(tud.Dataset):

    def __init__(self, data_json, preproc):

        data = read_data_json(data_json)
        self.preproc = preproc

        # chunk recordings into 4 different buckets according to length of text
        # ToDo: whats the point? Anyway flattened at the end
        bucket_diff = 4
        max_len = max(len(x['text']) for x in data)
        num_buckets = max_len // bucket_diff
        buckets = [[] for _ in range(num_buckets)]
        for d in data:
            bid = min(len(d['text']) // bucket_diff, num_buckets - 1)
            buckets[bid].append(d)

        # Sort by input length followed by output length
        sort_fn = lambda x: (round(x['duration'], 1),
                             len(x['text']))
        for b in buckets:
            b.sort(key=sort_fn)
        # flatten the data
        data = [d for b in buckets for d in b]
        self.data = data

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        datum = self.data[idx]
        datum = self.preproc.preprocess(datum["audio"],
                                        datum["text"])
        return datum


class BatchRandomSampler(tud.sampler.Sampler):
    """
    Batches the data consecutively and randomly samples
    by batch without replacement.
    """

    def __init__(self, data_source, batch_size):
        it_end = len(data_source) - batch_size + 1
        self.batches = [range(i, i + batch_size)
                        for i in range(0, it_end, batch_size)]
        self.data_source = data_source

    def __iter__(self):
        random.shuffle(self.batches)
        return (i for b in self.batches for i in b)

    def __len__(self):
        return len(self.data_source)


def make_loader(dataset_json, preproc, batch_size, num_workers=4):
    dataset = AudioDataset(dataset_json, preproc)
    sampler = BatchRandomSampler(dataset, batch_size)
    loader = tud.DataLoader(dataset,
                            batch_size=batch_size,
                            sampler=sampler,
                            num_workers=num_workers,
                            collate_fn=lambda batch: zip(*batch),
                            drop_last=True)
    return loader


def log_specgram_from_file(config, audio_file):
    audio, fs = io.array_from_wave(audio_file)
    assert fs == config['fs']
    return log_specgram(config, audio)


def log_specgram(config, audio, eps=1e-10):
    window_size, step_size = config['win_size'], config['step_size']
    sample_rate = config['fs']
    nperseg = int(window_size * sample_rate / 1e3)
    noverlap = int(step_size * sample_rate / 1e3)
    spec = torchaudio.transforms.Spectrogram(n_fft=nperseg,
                                             win_length=nperseg,
                                             hop_length=noverlap,
                                             window_fn=torch.hann_window)
    return torch.log(spec(audio) + eps)


def read_data_json(data_json):
    with open(data_json) as fid:
        return [json.loads(l) for l in fid]
