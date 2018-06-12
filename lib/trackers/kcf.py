from __future__ import absolute_import, division

import numpy as np
import cv2

from . import Tracker
from ..utils import dict2tuple
from ..utils.complex import real, fft, ifft, complex_mul, complex_div, circ_shift
from ..descriptors.fhog import fast_hog


class TrackerKCF(Tracker):

    def __init__(self, **kargs):
        super(TrackerKCF, self).__init__('KCF')
        self.parse_args(**kargs)

    def parse_args(self, **kargs):
        self.cfg = {
            'lambda_': 1e-4,
            'padding': 1.5,
            'output_sigma_factor': 0.125,
            'interp_factor': 0.012,
            'sigma': 0.6,
            'cell_size': 4}

        for key, val in kargs.items():
            self.cfg.update({key: val})
        self.cfg = dict2tuple(self.cfg)

    def init(self, image, init_rect):
        # initialize parameters
        self.t_center = init_rect[:2] + init_rect[2:] / 2
        self.t_sz = init_rect[2:]
        mod = self.cfg.cell_size * 2
        self.padded_sz = self.t_sz * (1 + self.cfg.padding)
        self.padded_sz = self.padded_sz.astype(int) // mod * mod + mod

        # get feature size and initialize hanning window
        self.z = self._crop(
            image, self.t_center, self.padded_sz)
        self.z = fast_hog(self.z, self.cfg.cell_size)
        self.feat_sz = self.z.shape
        self.hann_window = np.outer(
            np.hanning(self.feat_sz[0]),
            np.hanning(self.feat_sz[1])).astype(np.float32)
        self.hann_window = self.hann_window[:, :, np.newaxis]
        self.z *= self.hann_window

        # create gaussian labels
        output_sigma = self.cfg.output_sigma_factor * \
            np.sqrt(np.prod(self.feat_sz[:2])) / (1 + self.cfg.padding)
        rs, cs = np.ogrid[:self.feat_sz[0], :self.feat_sz[1]]
        rs, cs = rs - self.feat_sz[0] // 2, cs - self.feat_sz[1] // 2
        y = np.exp(-0.5 / output_sigma ** 2 * (rs ** 2 + cs ** 2))
        self.yf = fft(y)

        # train classifier
        k = self._gaussian_correlation(self.z, self.z)
        self.alphaf = complex_div(self.yf, fft(k) + self.cfg.lambda_)

    def update(self, image):
        self.t_center = np.clip(
            self.t_center, -self.t_sz / 2 + 1,
            image.shape[1::-1] + self.t_sz / 2 - 2)

        # locate target
        x = self._crop(image, self.t_center, self.padded_sz)
        x = self.hann_window * fast_hog(x, self.cfg.cell_size)
        k = self._gaussian_correlation(x, self.z)
        score = real(ifft(complex_mul(self.alphaf, fft(k))))
        offset = self._locate_target(score)
        self.t_center += offset * self.cfg.cell_size
        # limit the estimated bounding box to be overlapped with the image
        self.t_center = np.clip(
            self.t_center, -self.t_sz / 2 + 2,
            image.shape[1::-1] + self.t_sz / 2 - 1)

        # update model
        new_z = self._crop(image, self.t_center, self.padded_sz)
        new_z = self.hann_window * fast_hog(new_z, self.cfg.cell_size)
        k = self._gaussian_correlation(new_z, new_z)
        new_alphaf = complex_div(self.yf, fft(k) + self.cfg.lambda_)
        self.alphaf = (1 - self.cfg.interp_factor) * self.alphaf + \
            self.cfg.interp_factor * new_alphaf
        self.z = (1 - self.cfg.interp_factor) * self.z + \
            self.cfg.interp_factor * new_z

        bndbox = np.concatenate([
            self.t_center - self.t_sz / 2, self.t_sz])

        return bndbox

    def _crop(self, image, center, size):
        corners = np.zeros(4, dtype=int)
        corners[:2] = np.floor(center - size / 2).astype(int)
        corners[2:] = corners[:2] + size
        pads = np.concatenate(
            (-corners[:2], corners[2:] - image.shape[1::-1]))
        pads = np.maximum(0, pads)

        if np.any(pads > 0):
            corners = np.concatenate((
                corners[:2] + pads[:2],
                corners[2:] - pads[2:])).astype(int)

        patch = image[corners[1]:corners[3], corners[0]:corners[2]]

        if np.any(pads > 0):
            patch = cv2.copyMakeBorder(
                patch, pads[1], pads[3], pads[0], pads[2],
                borderType=cv2.BORDER_REPLICATE)

        return patch

    def _gaussian_correlation(self, x1, x2):
        xcorr = np.zeros((self.feat_sz[0], self.feat_sz[1]), np.float32)
        for i in range(self.feat_sz[2]):
            xcorr_ = cv2.mulSpectrums(fft(x1[:, :, i]), fft(x2[:, :, i]), 0, conjB=True)
            xcorr_ = real(ifft(xcorr_))
            xcorr += xcorr_
        xcorr = circ_shift(xcorr)

        d = (np.sum(x1 * x1) + np.sum(x2 * x2) - 2.0 * xcorr) / \
            (self.feat_sz[0]*self.feat_sz[1]*self.feat_sz[2])
        d = d * (d >= 0)
        d = np.exp(-d / (self.cfg.sigma*self.cfg.sigma))

        return d

    def _locate_target(self, score):
        def subpixel_peak(left, center, right):
            divisor = 2 * center - left - right
            if abs(divisor) < 1e-3:
                return 0
            return 0.5 * (right - left) / divisor

        _, _, _, max_loc = cv2.minMaxLoc(score)
        loc = np.float32(max_loc)

        if max_loc[0] in range(1, score.shape[1] - 1):
            loc[0] += subpixel_peak(
                score[max_loc[1], max_loc[0] - 1],
                score[max_loc[1], max_loc[0]],
                score[max_loc[1], max_loc[0] + 1])
        if max_loc[1] in range(1, score.shape[0] - 1):
            loc[1] += subpixel_peak(
                score[max_loc[1] - 1, max_loc[0]],
                score[max_loc[1], max_loc[0]],
                score[max_loc[1] + 1, max_loc[0]])
        offset = loc - np.float32(score.shape[1::-1]) / 2

        return offset
