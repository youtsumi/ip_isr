#
# LSST Data Management System
# Copyright 2008-2017 AURA/LSST.
#
# This product includes software developed by the
# LSST Project (http://www.lsst.org/).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the LSST License Statement and
# the GNU General Public License along with this program.  If not,
# see <https://www.lsstcorp.org/LegalNotices/>.
#

import copy
import unittest
import numpy as np
import logging
import galsim
from scipy.stats import median_abs_deviation

import lsst.geom as geom
import lsst.ip.isr.isrMockLSST as isrMockLSST
import lsst.utils.tests
from lsst.ip.isr.isrTaskLSST import (IsrTaskLSST, IsrTaskLSSTConfig)
from lsst.ip.isr.crosstalk import CrosstalkCalib
from lsst.ip.isr import PhotonTransferCurveDataset


class IsrTaskLSSTTestCase(lsst.utils.tests.TestCase):
    """Test IsrTaskLSST"""
    def setUp(self):
        mock = isrMockLSST.IsrMockLSST()
        self.camera = mock.getCamera()
        self.detector = self.camera[mock.config.detectorIndex]
        self.namp = len(self.detector)

        # Create adu (bootstrap) calibration frames
        self.bias_adu = isrMockLSST.BiasMockLSST(adu=True).run()
        self.dark_adu = isrMockLSST.DarkMockLSST(adu=True).run()
        self.flat_adu = isrMockLSST.FlatMockLSST(adu=True).run()

        # Create calibration frames
        self.bias = isrMockLSST.BiasMockLSST().run()
        self.dark = isrMockLSST.DarkMockLSST().run()
        self.flat = isrMockLSST.FlatMockLSST().run()
        self.bf_kernel = isrMockLSST.BfKernelMockLSST().run()

        # The crosstalk ratios in isrMockLSST are in electrons.
        self.crosstalk = CrosstalkCalib(nAmp=self.namp)
        self.crosstalk.hasCrosstalk = True
        self.crosstalk.coeffs = isrMockLSST.CrosstalkCoeffMockLSST().run()
        for i, amp in enumerate(self.detector):
            self.crosstalk.fitGains[i] = mock.config.gainDict[amp.getName()]
        self.crosstalk.crosstalkRatiosUnits = "electron"

        self.defects = isrMockLSST.DefectMockLSST().run()

        amp_names = [x.getName() for x in self.detector.getAmplifiers()]
        self.ptc = PhotonTransferCurveDataset(amp_names,
                                              ptcFitType='DUMMY_PTC',
                                              covMatrixSide=1)

        # PTC records noise units in electron, same as the
        # configuration parameter.
        for amp_name in amp_names:
            self.ptc.gain[amp_name] = mock.config.gainDict.get(amp_name, mock.config.gain)
            self.ptc.noise[amp_name] = mock.config.readNoise

        # TODO:
        # self.cti = isrMockLSST.DeferredChargeMockLSST().run()

        self.linearizer = isrMockLSST.LinearizerMockLSST().run()
        # We currently only have high-signal non-linearity.
        mock_config = self.get_mock_config_no_signal()
        for amp_name in amp_names:
            coeffs = self.linearizer.linearityCoeffs[amp_name]
            centers, values = np.split(coeffs, 2)
            values[centers < mock_config.highSignalNonlinearityThreshold] = 0.0
            self.linearizer.linearityCoeffs[amp_name] = np.concatenate((centers, values))

        self.saturation_adu = 100_000.0

    def test_isrBootstrapBias(self):
        """Test processing of a ``bootstrap`` bias frame.

        This will be output with ADU units.
        """
        mock_config = self.get_mock_config_no_signal()

        mock = isrMockLSST.IsrMockLSST(config=mock_config)
        input_exp = mock.run()

        isr_config = self.get_isr_config_minimal_corrections()
        isr_config.doBootstrap = True
        isr_config.doApplyGains = False
        isr_config.doBias = True
        isr_config.doCrosstalk = True

        # Need to make sure we are not masking the negative variance
        # pixels when directly comparing calibration images and
        # calibration-corrected calibrations.
        isr_config.maskNegativeVariance = False

        isr_task = IsrTaskLSST(config=isr_config)
        with self.assertLogs(level=logging.WARNING) as cm:
            result = isr_task.run(
                input_exp.clone(),
                bias=self.bias_adu,
                ptc=self.ptc,
                crosstalk=self.crosstalk,
            )
        self.assertIn("Ignoring provided PTC", cm.output[0])

        # Rerun without doing the bias correction.
        isr_config.doBias = False
        isr_task2 = IsrTaskLSST(config=isr_config)
        with self.assertNoLogs(level=logging.WARNING):
            result2 = isr_task2.run(input_exp.clone(), crosstalk=self.crosstalk)

        good_pixels = self.get_non_defect_pixels(result.exposure.mask)

        self.assertLess(
            np.mean(result.exposure.image.array[good_pixels]),
            np.mean(result2.exposure.image.array[good_pixels]),
        )
        self.assertLess(
            np.std(result.exposure.image.array[good_pixels]),
            np.std(result2.exposure.image.array[good_pixels]),
        )

        delta = result2.exposure.image.array - result.exposure.image.array
        self.assertFloatsAlmostEqual(delta[good_pixels], self.bias_adu.image.array[good_pixels], atol=1e-5)

        metadata = result.exposure.metadata

        key = "LSST ISR BOOTSTRAP"
        self.assertIn(key, metadata)
        self.assertEqual(metadata[key], True)

        key = "LSST ISR UNITS"
        self.assertIn(key, metadata)
        self.assertEqual(metadata[key], "adu")

        key = "LSST ISR READNOISE UNITS"
        self.assertIn(key, metadata)
        self.assertEqual(metadata[key], "electron")

        for amp in self.detector:
            amp_name = amp.getName()
            key = f"LSST ISR GAIN {amp_name}"
            self.assertIn(key, metadata)
            self.assertEqual(metadata[key], 1.0)
            key = f"LSST ISR SATURATION LEVEL {amp_name}"
            self.assertIn(key, metadata)
            self.assertEqual(metadata[key], self.saturation_adu)

        self._check_bad_column_crosstalk_correction(result.exposure)

    def test_isrBootstrapDark(self):
        """Test processing of a ``bootstrap`` dark frame.

        This will be output with ADU units.
        """
        mock_config = self.get_mock_config_no_signal()
        mock_config.doAddDark = True

        mock = isrMockLSST.IsrMockLSST(config=mock_config)
        input_exp = mock.run()

        isr_config = self.get_isr_config_minimal_corrections()
        isr_config.doBootstrap = True
        isr_config.doApplyGains = False
        isr_config.doBias = True
        isr_config.doDark = True
        isr_config.maskNegativeVariance = False
        isr_config.doCrosstalk = True

        isr_task = IsrTaskLSST(config=isr_config)
        with self.assertLogs(level=logging.WARNING) as cm:
            result = isr_task.run(
                input_exp.clone(),
                bias=self.bias_adu,
                dark=self.dark_adu,
                ptc=self.ptc,
                crosstalk=self.crosstalk,
            )
        self.assertIn("Ignoring provided PTC", cm.output[0])

        # Rerun without doing the dark correction.
        isr_config.doDark = False
        isr_task2 = IsrTaskLSST(config=isr_config)
        with self.assertNoLogs(level=logging.WARNING):
            result2 = isr_task2.run(input_exp.clone(), bias=self.bias_adu, crosstalk=self.crosstalk)

        good_pixels = self.get_non_defect_pixels(result.exposure.mask)

        self.assertLess(
            np.mean(result.exposure.image.array[good_pixels]),
            np.mean(result2.exposure.image.array[good_pixels]),
        )

        delta = result2.exposure.image.array - result.exposure.image.array
        exp_time = input_exp.getInfo().getVisitInfo().getExposureTime()
        self.assertFloatsAlmostEqual(
            delta[good_pixels],
            self.dark_adu.image.array[good_pixels] * exp_time,
            atol=1e-5,
        )

        metadata = result.exposure.metadata

        key = "LSST ISR BOOTSTRAP"
        self.assertIn(key, metadata)
        self.assertEqual(metadata[key], True)

        key = "LSST ISR UNITS"
        self.assertIn(key, metadata)
        self.assertEqual(metadata[key], "adu")

        self._check_bad_column_crosstalk_correction(result.exposure)

    def test_isrBootstrapFlat(self):
        """Test processing of a ``bootstrap`` flat frame.

        This will be output with ADU units.
        """
        mock_config = self.get_mock_config_no_signal()
        mock_config.doAddDark = True
        mock_config.doAddFlat = True
        # The doAddSky option adds the equivalent of flat-field flux.
        mock_config.doAddSky = True

        mock = isrMockLSST.IsrMockLSST(config=mock_config)
        input_exp = mock.run()

        isr_config = self.get_isr_config_minimal_corrections()
        isr_config.doBootstrap = True
        isr_config.doApplyGains = False
        isr_config.doBias = True
        isr_config.doDark = True
        isr_config.doFlat = True
        isr_config.maskNegativeVariance = False
        isr_config.doCrosstalk = True

        isr_task = IsrTaskLSST(config=isr_config)
        with self.assertLogs(level=logging.WARNING) as cm:
            result = isr_task.run(
                input_exp.clone(),
                bias=self.bias_adu,
                dark=self.dark_adu,
                flat=self.flat_adu,
                ptc=self.ptc,
                crosstalk=self.crosstalk,
            )
        self.assertIn("Ignoring provided PTC", cm.output[0])

        # Rerun without doing the flat correction.
        isr_config.doFlat = False
        isr_task2 = IsrTaskLSST(config=isr_config)
        with self.assertNoLogs(level=logging.WARNING):
            result2 = isr_task2.run(
                input_exp.clone(),
                bias=self.bias_adu,
                dark=self.dark_adu,
                crosstalk=self.crosstalk,
            )

        good_pixels = self.get_non_defect_pixels(result.exposure.mask)

        # Applying the flat will increase the counts.
        self.assertGreater(
            np.mean(result.exposure.image.array[good_pixels]),
            np.mean(result2.exposure.image.array[good_pixels]),
        )
        # And will decrease the sigma.
        self.assertLess(
            np.std(result.exposure.image.array[good_pixels]),
            np.std(result2.exposure.image.array[good_pixels]),
        )

        ratio = result2.exposure.image.array / result.exposure.image.array
        self.assertFloatsAlmostEqual(ratio[good_pixels], self.flat_adu.image.array[good_pixels], atol=1e-5)

        # Test the variance plane in the case of adu units.
        # The expected variance starts with the image array.
        expected_variance = result.exposure.image.clone()
        # We have to remove the flat-fielding from the image pixels.
        expected_variance.array *= self.flat_adu.image.array
        # And add the gain and read noise (in electron) per amp.
        for amp in self.detector:
            # We need to use the gain and read noise from the header
            # because these are bootstraps.
            gain = result.exposure.metadata[f"LSST ISR GAIN {amp.getName()}"]
            read_noise = result.exposure.metadata[f"LSST ISR READNOISE {amp.getName()}"]

            expected_variance[amp.getBBox()].array /= gain
            # Read noise is always in electron units, but since this is a
            # bootstrap, the gain is 1.0.
            expected_variance[amp.getBBox()].array += (read_noise/gain)**2.
        # And apply the flat-field squared.
        expected_variance.array /= self.flat_adu.image.array**2.

        self.assertFloatsAlmostEqual(
            result.exposure.variance.array[good_pixels],
            expected_variance.array[good_pixels],
            rtol=1e-6,
        )

        metadata = result.exposure.metadata

        key = "LSST ISR BOOTSTRAP"
        self.assertIn(key, metadata)
        self.assertEqual(metadata[key], True)

        key = "LSST ISR UNITS"
        self.assertIn(key, metadata)
        self.assertEqual(metadata[key], "adu")

        self._check_bad_column_crosstalk_correction(result.exposure)

    def test_isrBias(self):
        """Test processing of a bias frame."""
        mock_config = self.get_mock_config_no_signal()

        mock = isrMockLSST.IsrMockLSST(config=mock_config)
        input_exp = mock.run()

        isr_config = self.get_isr_config_electronic_corrections()
        isr_config.doBias = True
        # We do not do defect correction when processing biases.
        isr_config.doDefect = False
        isr_config.maskNegativeVariance = False

        isr_task = IsrTaskLSST(config=isr_config)
        with self.assertNoLogs(level=logging.WARNING):
            result = isr_task.run(
                input_exp.clone(),
                bias=self.bias,
                crosstalk=self.crosstalk,
                ptc=self.ptc,
                linearizer=self.linearizer,
            )

        # Rerun without doing the bias correction.
        isr_config.doBias = False
        isr_task2 = IsrTaskLSST(config=isr_config)
        with self.assertNoLogs(level=logging.WARNING):
            result2 = isr_task2.run(
                input_exp.clone(),
                crosstalk=self.crosstalk,
                ptc=self.ptc,
                linearizer=self.linearizer,
            )

        good_pixels = self.get_non_defect_pixels(result.exposure.mask)

        self.assertLess(
            np.mean(result.exposure.image.array[good_pixels]),
            np.mean(result2.exposure.image.array[good_pixels]),
        )

        self.assertLess(
            np.std(result.exposure.image.array[good_pixels]),
            np.std(result2.exposure.image.array[good_pixels]),
        )

        # Confirm that it is flat with an arbitrary cutoff that depends
        # on the read noise.
        self.assertLess(np.std(result.exposure.image.array[good_pixels]), 2.0*mock_config.readNoise)

        delta = result2.exposure.image.array - result.exposure.image.array

        # Note that the bias is made with bias noise + read noise, and
        # the image contains read noise.
        self.assertFloatsAlmostEqual(
            delta[good_pixels],
            self.bias.image.array[good_pixels],
            atol=1e-5,
        )

        self._check_bad_column_crosstalk_correction(result.exposure)

    def test_isrDark(self):
        """Test processing of a dark frame."""
        mock_config = self.get_mock_config_no_signal()
        mock_config.doAddDark = True
        # We turn off the bad parallel overscan column because it does
        # add more noise to that region.
        mock_config.doAddBadParallelOverscanColumn = False

        mock = isrMockLSST.IsrMockLSST(config=mock_config)
        input_exp = mock.run()

        isr_config = self.get_isr_config_electronic_corrections()
        isr_config.doBias = True
        isr_config.doDark = True
        # We do not do defect correction when processing darks.
        isr_config.doDefect = False
        isr_config.maskNegativeVariance = False

        isr_task = IsrTaskLSST(config=isr_config)
        with self.assertNoLogs(level=logging.WARNING):
            result = isr_task.run(
                input_exp.clone(),
                bias=self.bias,
                dark=self.dark,
                crosstalk=self.crosstalk,
                ptc=self.ptc,
                linearizer=self.linearizer,
            )

        # Rerun without doing the dark correction.
        isr_config.doDark = False
        isr_task2 = IsrTaskLSST(config=isr_config)
        with self.assertNoLogs(level=logging.WARNING):
            result2 = isr_task2.run(
                input_exp.clone(),
                bias=self.bias,
                crosstalk=self.crosstalk,
                ptc=self.ptc,
                linearizer=self.linearizer,
            )

        good_pixels = self.get_non_defect_pixels(result.exposure.mask)

        self.assertLess(
            np.mean(result.exposure.image.array[good_pixels]),
            np.mean(result2.exposure.image.array[good_pixels]),
        )
        # The mock dark has no noise, so these should be equal.
        self.assertFloatsAlmostEqual(
            np.std(result.exposure.image.array[good_pixels]),
            np.std(result2.exposure.image.array[good_pixels]),
            atol=1e-12,
        )

        # This is a somewhat arbitrary comparison that includes a fudge
        # factor for the extra noise from the overscan subtraction.
        self.assertLess(
            np.std(result.exposure.image.array[good_pixels]),
            1.6*np.sqrt(mock_config.darkRate*mock_config.expTime + mock_config.readNoise),
        )

        delta = result2.exposure.image.array - result.exposure.image.array
        exp_time = input_exp.getInfo().getVisitInfo().getExposureTime()
        self.assertFloatsAlmostEqual(
            delta[good_pixels],
            self.dark.image.array[good_pixels] * exp_time,
            atol=1e-12,
        )

        self._check_bad_column_crosstalk_correction(result.exposure)

    def test_isrFlat(self):
        """Test processing of a flat frame."""
        mock_config = self.get_mock_config_no_signal()
        mock_config.doAddDark = True
        mock_config.doAddFlat = True
        # The doAddSky option adds the equivalent of flat-field flux.
        mock_config.doAddSky = True

        mock = isrMockLSST.IsrMockLSST(config=mock_config)
        input_exp = mock.run()

        isr_config = self.get_isr_config_electronic_corrections()
        isr_config.doBias = True
        isr_config.doDark = True
        isr_config.doFlat = True
        # Although we usually do not do defect interpolation when
        # processing flats, this is a good test of the interpolation.
        isr_config.doDefect = True
        isr_config.maskNegativeVariance = False

        isr_task = IsrTaskLSST(config=isr_config)
        with self.assertNoLogs(level=logging.WARNING):
            result = isr_task.run(
                input_exp.clone(),
                bias=self.bias,
                dark=self.dark,
                flat=self.flat,
                crosstalk=self.crosstalk,
                defects=self.defects,
                ptc=self.ptc,
                linearizer=self.linearizer,
            )

        # Rerun without doing the bias correction.
        isr_config.doFlat = False
        isr_task2 = IsrTaskLSST(config=isr_config)
        with self.assertNoLogs(level=logging.WARNING):
            result2 = isr_task2.run(
                input_exp.clone(),
                bias=self.bias,
                dark=self.dark,
                crosstalk=self.crosstalk,
                defects=self.defects,
                ptc=self.ptc,
                linearizer=self.linearizer,
            )

        # With defect correction, we should not need to filter out bad
        # pixels.

        # Applying the flat will increase the counts.
        self.assertGreater(
            np.mean(result.exposure.image.array),
            np.mean(result2.exposure.image.array),
        )
        # And will decrease the sigma.
        self.assertLess(
            np.std(result.exposure.image.array),
            np.std(result2.exposure.image.array),
        )

        # Check that the resulting image is approximately flat.
        # In particular that the noise is consistent with sky + margin.
        self.assertLess(np.std(result.exposure.image.array), np.sqrt(mock_config.skyLevel) + 3.0)

        # Generate a flat without any defects for comparison
        # (including interpolation)
        flat_nodefect_config = isrMockLSST.FlatMockLSST.ConfigClass()
        flat_nodefect_config.doAddBrightDefects = False
        flat_nodefects = isrMockLSST.FlatMockLSST(config=flat_nodefect_config).run()

        ratio = result2.exposure.image.array / result.exposure.image.array
        self.assertFloatsAlmostEqual(ratio, flat_nodefects.image.array, atol=1e-4)

        self._check_bad_column_crosstalk_correction(result.exposure)

    def test_isrNoise(self):
        """Test the recorded noise and gain in the metadata."""
        mock_config = self.get_mock_config_no_signal()
        # Remove the overscan scale so that the only variation
        # in the overscan is from the read noise.
        mock_config.overscanScale = 0.0

        mock = isrMockLSST.IsrMockLSST(config=mock_config)
        input_exp = mock.run()

        isr_config = self.get_isr_config_electronic_corrections()
        isr_config.doBias = True
        # We do not do defect correction when processing biases.
        isr_config.doDefect = False
        isr_config.maskNegativeVariance = False

        isr_task = IsrTaskLSST(config=isr_config)
        with self.assertNoLogs(level=logging.WARNING):
            result = isr_task.run(
                input_exp.clone(),
                bias=self.bias,
                crosstalk=self.crosstalk,
                ptc=self.ptc,
                linearizer=self.linearizer,
            )

        metadata = result.exposure.metadata

        for amp in self.detector:
            # The overscan noise is always in adu and the readnoise is always
            # in electron.
            gain = result.exposure.metadata[f"LSST ISR GAIN {amp.getName()}"]
            read_noise = result.exposure.metadata[f"LSST ISR READNOISE {amp.getName()}"]

            # Check that the gain and read noise are consistent with the
            # values stored in the PTC.
            self.assertEqual(gain, self.ptc.gain[amp.getName()])
            self.assertEqual(read_noise, self.ptc.noise[amp.getName()])

            key = f"LSST ISR OVERSCAN RESIDUAL SERIAL STDEV {amp.getName()}"
            self.assertIn(key, metadata)

            # Determine if the residual serial overscan stddev is consistent
            # with the PTC readnoise within 3xstandard error.
            serial_overscan_area = amp.getRawHorizontalOverscanBBox().area
            self.assertFloatsAlmostEqual(
                metadata[key] * gain,
                read_noise,
                atol=3*read_noise / np.sqrt(serial_overscan_area),
            )

    def test_isrBrighterFatter(self):
        """Test processing of a flat frame."""
        # Image with brighter-fatter correction
        mock_config = self.get_mock_config_no_signal()
        mock_config.isTrimmed = False
        mock_config.doAddDark = True
        mock_config.doAddFlat = True
        mock_config.doAddSky = True
        mock_config.doAddSource = True
        mock_config.sourceFlux = [75000.0]
        mock_config.doAddBrighterFatter = True

        mock = isrMockLSST.IsrMockLSST(config=mock_config)
        input_exp = mock.run()

        isr_config = self.get_isr_config_electronic_corrections()
        isr_config.doBias = True
        isr_config.doDark = True
        isr_config.doFlat = True
        isr_config.doBrighterFatter = True

        isr_task = IsrTaskLSST(config=isr_config)
        with self.assertNoLogs(level=logging.WARNING):
            result = isr_task.run(
                input_exp.clone(),
                bias=self.bias,
                dark=self.dark,
                flat=self.flat,
                crosstalk=self.crosstalk,
                defects=self.defects,
                ptc=self.ptc,
                linearizer=self.linearizer,
                bfKernel=self.bf_kernel,
            )

        mock_config = self.get_mock_config_no_signal()
        mock_config.isTrimmed = False
        mock_config.doAddDark = True
        mock_config.doAddFlat = True
        mock_config.doAddSky = True
        mock_config.doAddSource = True
        mock_config.sourceFlux = [75000.0]
        mock_config.doAddBrighterFatter = False

        mock = isrMockLSST.IsrMockLSST(config=mock_config)
        input_truth = mock.run()

        isr_config = self.get_isr_config_electronic_corrections()
        isr_config.doBias = True
        isr_config.doDark = True
        isr_config.doFlat = True
        isr_config.doBrighterFatter = False

        isr_task = IsrTaskLSST(config=isr_config)
        with self.assertNoLogs(level=logging.WARNING):
            result_truth = isr_task.run(
                input_truth.clone(),
                bias=self.bias,
                dark=self.dark,
                flat=self.flat,
                crosstalk=self.crosstalk,
                defects=self.defects,
                ptc=self.ptc,
                linearizer=self.linearizer,
                bfKernel=self.bf_kernel,
            )

        # Measure the source size in the BF-corrected image.
        # The injected source is a Gaussian with 3.0px
        image = galsim.ImageF(result.exposure.image.array)
        image_truth = galsim.ImageF(result_truth.exposure.image.array)
        source_centroid = galsim.PositionD(mock_config.sourceX[0], mock_config.sourceY[0])
        hsm_result = galsim.hsm.FindAdaptiveMom(image, guess_centroid=source_centroid, strict=False)
        hsm_result_truth = galsim.hsm.FindAdaptiveMom(image_truth, guess_centroid=source_centroid,
                                                      strict=False)
        measured_sigma = hsm_result.moments_sigma
        true_sigma = hsm_result_truth.moments_sigma
        self.assertFloatsAlmostEqual(measured_sigma, true_sigma, rtol=3e-3)

        # Check that the variance in an amp far away from the
        # source is expected. The source is in amp 0; this will
        # check the variation in neighboring amp 1
        test_amp_bbox = result.exposure.detector.getAmplifiers()[1].getBBox()
        n_pixels = test_amp_bbox.getArea()
        stdev = np.std(result.exposure[test_amp_bbox].image.array)
        stdev_truth = np.std(result_truth.exposure[test_amp_bbox].image.array)
        self.assertFloatsAlmostEqual(stdev, stdev_truth, atol=3*stdev_truth/np.sqrt(n_pixels))

        # Check that the variance in the amp with a defect is
        # unchanged as a result of applying the BF correction after
        # interpolating. The defect was added to amplifier 2.
        test_amp_bbox = result.exposure.detector.getAmplifiers()[2].getBBox()
        good_pixels = self.get_non_defect_pixels(result.exposure[test_amp_bbox].mask)
        stdev = np.nanstd(result.exposure[test_amp_bbox].image.array[good_pixels])
        stdev_truth = np.nanstd(result_truth.exposure[test_amp_bbox].image.array[good_pixels])
        self.assertFloatsAlmostEqual(stdev, stdev_truth, atol=3*stdev_truth/np.sqrt(n_pixels))

    def test_isrSkyImage(self):
        """Test processing of a sky image."""
        mock_config = self.get_mock_config_no_signal()
        mock_config.doAddDark = True
        mock_config.doAddFlat = True
        # Set this to False until we have fringe correction.
        mock_config.doAddFringe = False
        mock_config.doAddSky = True
        mock_config.doAddSource = True

        mock = isrMockLSST.IsrMockLSST(config=mock_config)
        input_exp = mock.run()

        isr_config = self.get_isr_config_electronic_corrections()
        isr_config.doBias = True
        isr_config.doDark = True
        isr_config.doFlat = True

        isr_task = IsrTaskLSST(config=isr_config)
        with self.assertNoLogs(level=logging.WARNING):
            result = isr_task.run(
                input_exp.clone(),
                bias=self.bias,
                dark=self.dark,
                flat=self.flat,
                crosstalk=self.crosstalk,
                defects=self.defects,
                ptc=self.ptc,
                linearizer=self.linearizer,
            )

        # Confirm that the output has the defect line as bad.
        sat_val = 2**result.exposure.mask.getMaskPlane("BAD")
        for defect in self.defects:
            np.testing.assert_array_equal(
                result.exposure.mask[defect.getBBox()].array & sat_val,
                sat_val,
            )

        clean_mock_config = self.get_mock_config_clean()
        # We want the dark noise for more direct comparison.
        clean_mock_config.doAddDarkNoiseOnly = True
        clean_mock_config.doAddSky = True
        clean_mock_config.doAddSource = True

        clean_mock = isrMockLSST.IsrMockLSST(config=clean_mock_config)
        clean_exp = clean_mock.run()

        delta = result.exposure.image.array - clean_exp.image.array

        good_pixels = self.get_non_defect_pixels(result.exposure.mask)

        # We compare the good pixels in the entirety.
        self.assertLess(np.std(delta[good_pixels]), 5.0)
        self.assertLess(np.max(np.abs(delta[good_pixels])), 5.0*7)

        # Make sure the corrected image is overall consistent with the
        # straight image.
        self.assertLess(np.abs(np.median(delta[good_pixels])), 0.5)

        # And overall where the interpolation is a bit worse but
        # the statistics are still fine.
        self.assertLess(np.std(delta), 5.5)

        metadata = result.exposure.metadata

        key = "LSST ISR BOOTSTRAP"
        self.assertIn(key, metadata)
        self.assertEqual(metadata[key], False)

        key = "LSST ISR UNITS"
        self.assertIn(key, metadata)
        self.assertEqual(metadata[key], "electron")

        for amp in self.detector:
            amp_name = amp.getName()
            key = f"LSST ISR GAIN {amp_name}"
            self.assertIn(key, metadata)
            self.assertEqual(metadata[key], gain := self.ptc.gain[amp_name])
            key = f"LSST ISR READNOISE {amp_name}"
            self.assertIn(key, metadata)
            self.assertEqual(metadata[key], self.ptc.noise[amp_name])
            key = f"LSST ISR SATURATION LEVEL {amp_name}"
            self.assertIn(key, metadata)
            self.assertEqual(metadata[key], self.saturation_adu * gain)

        # Test the variance plane in the case of electron units.
        # The expected variance starts with the image array.
        expected_variance = result.exposure.image.clone()
        # We have to remove the flat-fielding from the image pixels.
        expected_variance.array *= self.flat.image.array
        # And add the read noise (in electrons) per amp.
        for amp in self.detector:
            gain = self.ptc.gain[amp.getName()]
            read_noise = self.ptc.noise[amp.getName()]

            # The image, read noise, and variance plane should all have
            # units of electrons, electrons, and electrons^2.
            expected_variance[amp.getBBox()].array += read_noise**2.
        # And apply the flat-field squared.
        expected_variance.array /= self.flat.image.array**2.

        self.assertFloatsAlmostEqual(
            result.exposure.variance.array[good_pixels],
            expected_variance.array[good_pixels],
            rtol=1e-6,
        )

    def test_isrSkyImageSaturated(self):
        """Test processing of a sky image.

        This variation uses saturated pixels instead of defects.

        This additionally tests the gain config override.
        """
        mock_config = self.get_mock_config_no_signal()
        mock_config.doAddDark = True
        mock_config.doAddFlat = True
        # Set this to False until we have fringe correction.
        mock_config.doAddFringe = False
        mock_config.doAddSky = True
        mock_config.doAddSource = True
        mock_config.brightDefectLevel = 170_000.0  # Above saturation.

        mock = isrMockLSST.IsrMockLSST(config=mock_config)
        input_exp = mock.run()

        isr_config = self.get_isr_config_electronic_corrections()
        isr_config.doBias = True
        isr_config.doDark = True
        isr_config.doFlat = True
        # We turn off defect masking to test the saturation code.
        # However, the same pixels below should be masked/interpolated.
        isr_config.doDefect = False

        # This code will set the gain of one amp to the same as the ptc
        # value, and we will check that it is logged and used but the
        # results should be the same.
        detectorConfig = isr_config.overscanCamera.getOverscanDetectorConfig(self.detector)
        overscanAmpConfig = copy.copy(detectorConfig.defaultAmpConfig)
        overscanAmpConfig.gain = self.ptc.gain[self.detector[1].getName()]
        detectorConfig.ampRules[self.detector[1].getName()] = overscanAmpConfig

        isr_task = IsrTaskLSST(config=isr_config)
        with self.assertLogs(level=logging.WARNING) as cm:
            result = isr_task.run(
                input_exp.clone(),
                bias=self.bias,
                dark=self.dark,
                flat=self.flat,
                crosstalk=self.crosstalk,
                defects=self.defects,
                ptc=self.ptc,
                linearizer=self.linearizer,
            )
        self.assertIn("Overriding gain", cm.output[0])

        # Confirm that the output has the defect line as saturated.
        sat_val = 2**result.exposure.mask.getMaskPlane("SAT")
        for defect in self.defects:
            np.testing.assert_array_equal(
                result.exposure.mask[defect.getBBox()].array & sat_val,
                sat_val,
            )

        clean_mock_config = self.get_mock_config_clean()
        # We want the dark noise for more direct comparison.
        clean_mock_config.doAddDarkNoiseOnly = True
        clean_mock_config.doAddSky = True
        clean_mock_config.doAddSource = True

        clean_mock = isrMockLSST.IsrMockLSST(config=clean_mock_config)
        clean_exp = clean_mock.run()

        delta = result.exposure.image.array - clean_exp.image.array

        bad_val = 2**result.exposure.mask.getMaskPlane("BAD")
        good_pixels = np.where((result.exposure.mask.array & (sat_val | bad_val)) == 0)

        # We compare the good pixels in the entirety.
        self.assertLess(np.std(delta[good_pixels]), 5.0)
        # This is sensitive to parallel overscan masking.
        self.assertLess(np.max(np.abs(delta[good_pixels])), 5.0*7)

        # Make sure the corrected image is overall consistent with the
        # straight image.
        self.assertLess(np.abs(np.median(delta[good_pixels])), 0.5)

        # And overall where the interpolation is a bit worse but
        # the statistics are still fine.  Note that this is worse than
        # the defect case because of the widening of the saturation
        # trail.
        self.assertLess(np.std(delta), 7.0)

    def test_isrFloodedSaturatedE2V(self):
        """Test ISR when the amps are completely saturated.

        This version tests what happens when the parallel overscan
        region is flooded like E2V detectors, where the saturation
        spreads evenly, but at a greater level than the saturation
        value.
        """
        # We are simulating a flat field.
        # Note that these aren't very important because we are replacing
        # the flux, but we may as well.
        mock_config = self.get_mock_config_no_signal()
        mock_config.doAddDark = True
        mock_config.doAddFlat = True
        # The doAddSky option adds the equivalent of flat-field flux.
        mock_config.doAddSky = True

        mock = isrMockLSST.IsrMockLSST(config=mock_config)
        input_exp = mock.run()

        isr_config = self.get_isr_config_minimal_corrections()
        isr_config.doBootstrap = True
        isr_config.doApplyGains = False
        isr_config.doBias = True
        isr_config.doDark = True
        isr_config.doFlat = False
        # Tun off saturation masking to simulate a PTC flat.
        isr_config.doSaturation = False

        amp_config = isr_config.overscanCamera.defaultDetectorConfig.defaultAmpConfig
        parallel_overscan_saturation = amp_config.parallelOverscanConfig.parallelOverscanSaturationLevel

        detector = input_exp.getDetector()
        for i, amp in enumerate(detector):
            # For half of the amps we are testing what happens when the
            # parallel overscan region is above the configured saturation
            # level; for the other half we are testing the other branch
            # when it saturates below this level (which is a priori
            # unknown).
            if i < len(detector) // 2:
                data_level = (parallel_overscan_saturation * 1.05
                              + mock_config.biasLevel
                              + mock_config.clockInjectedOffsetLevel)
                parallel_overscan_level = (parallel_overscan_saturation * 1.1
                                           + mock_config.biasLevel
                                           + mock_config.clockInjectedOffsetLevel)
            else:
                data_level = (parallel_overscan_saturation * 0.7
                              + mock_config.biasLevel
                              + mock_config.clockInjectedOffsetLevel)
                parallel_overscan_level = (parallel_overscan_saturation * 0.75
                                           + mock_config.biasLevel
                                           + mock_config.clockInjectedOffsetLevel)

            input_exp[amp.getRawDataBBox()].image.array[:, :] = data_level
            input_exp[amp.getRawParallelOverscanBBox()].image.array[:, :] = parallel_overscan_level

        isr_task = IsrTaskLSST(config=isr_config)
        with self.assertLogs(level=logging.WARNING) as cm:
            result = isr_task.run(
                input_exp.clone(),
                bias=self.bias_adu,
                dark=self.dark_adu,
            )
        self.assertEqual(len(cm.records), len(detector))

        n_all = 0
        n_level = 0
        for record in cm.records:
            if "All overscan pixels masked" in record.message:
                n_all += 1
            if "The level in the overscan region" in record.message:
                n_level += 1

        self.assertEqual(n_all, len(detector) // 2)
        self.assertEqual(n_level, len(detector) // 2)

        # And confirm that the post-ISR levels are high for each amp.
        for amp in detector:
            med = np.median(result.exposure[amp.getBBox()].image.array)
            self.assertGreater(med, parallel_overscan_saturation*0.8)

    def test_isrFloodedSaturatedITL(self):
        """Test ISR when the amps are completely saturated.

        This version tests what happens when the parallel overscan
        region is flooded like ITL detectors, where the saturation
        is at a lower level than the imaging region, and also
        spreads partly into the serial/parallel region.
        """
        # We are simulating a flat field.
        # Note that these aren't very important because we are replacing
        # the flux, but we may as well.
        mock_config = self.get_mock_config_no_signal()
        mock_config.doAddDark = True
        mock_config.doAddFlat = True
        # The doAddSky option adds the equivalent of flat-field flux.
        mock_config.doAddSky = True

        mock = isrMockLSST.IsrMockLSST(config=mock_config)
        input_exp = mock.run()

        isr_config = self.get_isr_config_minimal_corrections()
        isr_config.doBootstrap = True
        isr_config.doApplyGains = False
        isr_config.doBias = True
        isr_config.doDark = True
        isr_config.doFlat = False
        # Tun off saturation masking to simulate a PTC flat.
        isr_config.doSaturation = False

        amp_config = isr_config.overscanCamera.defaultDetectorConfig.defaultAmpConfig
        parallel_overscan_saturation = amp_config.parallelOverscanConfig.parallelOverscanSaturationLevel

        detector = input_exp.getDetector()
        for i, amp in enumerate(detector):
            # For half of the amps we are testing what happens when the
            # parallel overscan region is above the configured saturation
            # level; for the other half we are testing the other branch
            # when it saturates below this level (which is a priori
            # unknown).
            if i < len(detector) // 2:
                data_level = (parallel_overscan_saturation * 1.1
                              + mock_config.biasLevel
                              + mock_config.clockInjectedOffsetLevel)
                parallel_overscan_level = (parallel_overscan_saturation * 1.05
                                           + mock_config.biasLevel
                                           + mock_config.clockInjectedOffsetLevel)
            else:
                data_level = (parallel_overscan_saturation * 0.75
                              + mock_config.biasLevel
                              + mock_config.clockInjectedOffsetLevel)
                parallel_overscan_level = (parallel_overscan_saturation * 0.7
                                           + mock_config.biasLevel
                                           + mock_config.clockInjectedOffsetLevel)

            input_exp[amp.getRawDataBBox()].image.array[:, :] = data_level
            input_exp[amp.getRawParallelOverscanBBox()].image.array[:, :] = parallel_overscan_level
            # The serial/parallel region for the test camera looks like this:
            serial_overscan_bbox = amp.getRawSerialOverscanBBox()
            parallel_overscan_bbox = amp.getRawParallelOverscanBBox()

            overscan_corner_bbox = geom.Box2I(
                geom.Point2I(
                    serial_overscan_bbox.getMinX(),
                    parallel_overscan_bbox.getMinY(),
                ),
                geom.Extent2I(
                    serial_overscan_bbox.getWidth(),
                    parallel_overscan_bbox.getHeight(),
                ),
            )
            input_exp[overscan_corner_bbox].image.array[-2:, :] = parallel_overscan_level

        isr_task = IsrTaskLSST(config=isr_config)
        with self.assertLogs(level=logging.WARNING) as cm:
            result = isr_task.run(
                input_exp.clone(),
                bias=self.bias_adu,
                dark=self.dark_adu,
            )
        self.assertEqual(len(cm.records), len(detector))

        n_all = 0
        n_level = 0
        for record in cm.records:
            if "All overscan pixels masked" in record.message:
                n_all += 1
            if "The level in the overscan region" in record.message:
                n_level += 1

        self.assertEqual(n_all, len(detector) // 2)
        self.assertEqual(n_level, len(detector) // 2)

        # And confirm that the post-ISR levels are high for each amp.
        for amp in detector:
            med = np.median(result.exposure[amp.getBBox()].image.array)
            self.assertGreater(med, parallel_overscan_saturation*0.8)

    def test_isrBadParallelOverscanColumnsBootstrap(self):
        """Test processing a bias when we have a bad parallel overscan column.

        This tests in bootstrap mode.
        """
        # We base this on the bootstrap bias, and make sure
        # that the bad column remains.
        mock_config = self.get_mock_config_no_signal()
        isr_config = self.get_isr_config_minimal_corrections()
        isr_config.doSaturation = False
        isr_config.doBootstrap = True
        isr_config.doApplyGains = False

        amp_config = isr_config.overscanCamera.defaultDetectorConfig.defaultAmpConfig
        overscan_sat_level_adu = amp_config.parallelOverscanConfig.parallelOverscanSaturationLevel
        # The defect is in amp 2.
        amp_gain = mock_config.gainDict[self.detector[2].getName()]
        overscan_sat_level = amp_gain * overscan_sat_level_adu
        # The expected defect level is in adu for the bootstrap bias.
        expected_defect_level = mock_config.brightDefectLevel / amp_gain

        # The levels are set in electron units.
        # We test 3 levels:
        #  * 1000.0, a lowish but outlier level.
        #  * 1.05*saturation.
        # Note that the default parallel overscan saturation level for
        # bootstrap (pre-saturation-measure) analysis is very low, in
        # order to capture all types of amps, even with low saturation.
        # Therefore, we only need to test above this saturation level.
        # (c.f. test_isrBadParallelOverscanColumns).
        levels = mock_config.clockInjectedOffsetLevel + np.array(
            [1000.0, 1.05*overscan_sat_level],
        )

        for level in levels:
            mock_config.badParallelOverscanColumnLevel = level
            mock = isrMockLSST.IsrMockLSST(config=mock_config)
            input_exp = mock.run()

            isr_task = IsrTaskLSST(config=isr_config)
            with self.assertNoLogs(level=logging.WARNING):
                result = isr_task.run(input_exp.clone())

            for defect in self.defects:
                bbox = defect.getBBox()
                defect_image = result.exposure[bbox].image.array

                # Check that the defect is the correct level
                # (not subtracted away).
                defect_median = np.median(defect_image)
                self.assertFloatsAlmostEqual(defect_median, expected_defect_level, rtol=1e-4)

                # Check that the neighbors aren't over-subtracted.
                for neighbor in [-1, 1]:
                    bbox_neighbor = bbox.shiftedBy(geom.Extent2I(neighbor, 0))
                    neighbor_image = result.exposure[bbox_neighbor].image.array

                    neighbor_median = np.median(neighbor_image)
                    self.assertFloatsAlmostEqual(neighbor_median, 0.0, atol=7.0)

    def test_isrBadParallelOverscanColumns(self):
        """Test processing a bias when we have a bad parallel overscan column.

        This test uses regular non-bootstrap processing.
        """
        mock_config = self.get_mock_config_no_signal()
        isr_config = self.get_isr_config_electronic_corrections()
        # We do not do defect correction when processing biases.
        isr_config.doDefect = False

        amp_config = isr_config.overscanCamera.defaultDetectorConfig.defaultAmpConfig
        sat_level_adu = amp_config.saturation
        # The defect is in amp 2.
        amp_gain = mock_config.gainDict[self.detector[2].getName()]
        sat_level = amp_gain * sat_level_adu
        # The expected defect level is in electron for the full bias.
        expected_defect_level = mock_config.brightDefectLevel

        # The levels are set in electron units.
        # We test 3 levels:
        #  * 1000.0, a lowish but outlier level.
        #  * 0.9*saturation, following ITL-style parallel overscan bleeds.
        #  * 1.05*saturation, following E2V-style parallel overscan bleeds.
        levels = mock_config.clockInjectedOffsetLevel + np.array(
            [1000.0, 0.9*sat_level, 1.1*sat_level],
        )

        for level in levels:
            mock_config.badParallelOverscanColumnLevel = level
            mock = isrMockLSST.IsrMockLSST(config=mock_config)
            input_exp = mock.run()

            isr_task = IsrTaskLSST(config=isr_config)
            with self.assertNoLogs(level=logging.WARNING):
                result = isr_task.run(
                    input_exp.clone(),
                    crosstalk=self.crosstalk,
                    ptc=self.ptc,
                    linearizer=self.linearizer,
                )

            for defect in self.defects:
                bbox = defect.getBBox()
                defect_image = result.exposure[bbox].image.array

                # Check that the defect is the correct level
                # (not subtracted away).
                defect_median = np.median(defect_image)
                self.assertFloatsAlmostEqual(defect_median, expected_defect_level, rtol=1e-4)

                # Check that the neighbors aren't over-subtracted.
                for neighbor in [-1, 1]:
                    bbox_neighbor = bbox.shiftedBy(geom.Extent2I(neighbor, 0))
                    neighbor_image = result.exposure[bbox_neighbor].image.array

                    neighbor_median = np.median(neighbor_image)
                    self.assertFloatsAlmostEqual(neighbor_median, 0.0, atol=7.0)

    def test_isrBadPtcGain(self):
        """Test processing when an amp has a bad (nan) PTC gain.
        """
        # We use a flat frame for this test for convenience.
        mock_config = self.get_mock_config_no_signal()
        mock_config.doAddDark = True
        mock_config.doAddFlat = True
        # The doAddSky option adds the equivalent of flat-field flux.
        mock_config.doAddSky = True

        mock = isrMockLSST.IsrMockLSST(config=mock_config)
        input_exp = mock.run()

        isr_config = self.get_isr_config_electronic_corrections()
        isr_config.doBias = True
        isr_config.doDark = True
        isr_config.doFlat = False
        isr_config.doDefect = True

        # Set a bad amplifier to a nan gain.
        bad_amp = self.detector[0].getName()

        ptc = copy.copy(self.ptc)
        ptc.gain[bad_amp] = np.nan

        # We also want non-zero (but very small) crosstalk values
        # to ensure that these don't propagate nans.
        crosstalk = copy.copy(self.crosstalk)
        for i in range(len(self.detector)):
            for j in range(len(self.detector)):
                if i == j:
                    continue
                if crosstalk.coeffs[i, j] == 0:
                    crosstalk.coeffs[i, j] = 1e-10

        isr_task = IsrTaskLSST(config=isr_config)
        with self.assertLogs(level=logging.WARNING) as cm:
            result = isr_task.run(
                input_exp.clone(),
                bias=self.bias,
                dark=self.dark,
                crosstalk=crosstalk,
                ptc=ptc,
                linearizer=self.linearizer,
                defects=self.defects,
            )
        self.assertIn(f"Amplifier {bad_amp} is bad (non-finite gain)", cm.output[0])

        # Confirm that the bad_amp is marked bad and the other amps are not.
        # We have to special case the amp with the defect.
        mask = result.exposure.mask

        for amp in self.detector:
            bbox = amp.getBBox()
            bad_in_amp = ((mask[bbox].array & 2**mask.getMaskPlaneDict()["BAD"]) > 0)

            if amp.getName() == bad_amp:
                self.assertTrue(np.all(bad_in_amp))
            elif amp.getName() == "C:0,2":
                # This is the amp with the defect.
                self.assertEqual(np.sum(bad_in_amp), 51)
            else:
                self.assertTrue(np.all(~bad_in_amp))

    def get_mock_config_no_signal(self):
        """Get an IsrMockLSSTConfig with all signal set to False.

        This will have all the electronic effects turned on (including
        2D bias).
        """
        mock_config = isrMockLSST.IsrMockLSSTConfig()
        mock_config.isTrimmed = False
        mock_config.doAddDark = False
        mock_config.doAddFlat = False
        mock_config.doAddFringe = False
        mock_config.doAddSky = False
        mock_config.doAddSource = False

        mock_config.doAdd2DBias = True
        mock_config.doAddBias = True
        mock_config.doAddCrosstalk = True
        mock_config.doAddBrightDefects = True
        mock_config.doAddClockInjectedOffset = True
        mock_config.doAddParallelOverscanRamp = True
        mock_config.doAddSerialOverscanRamp = True
        mock_config.doAddHighSignalNonlinearity = True
        mock_config.doApplyGain = True
        mock_config.doRoundAdu = True

        # We always want to generate the image with these configs.
        mock_config.doGenerateImage = True

        return mock_config

    def get_mock_config_clean(self):
        """Get an IsrMockLSSTConfig trimmed with all electronic signatures
        turned off.
        """
        mock_config = isrMockLSST.IsrMockLSSTConfig()
        mock_config.doAddBias = False
        mock_config.doAdd2DBias = False
        mock_config.doAddClockInjectedOffset = False
        mock_config.doAddDark = False
        mock_config.doAddDarkNoiseOnly = False
        mock_config.doAddFlat = False
        mock_config.doAddFringe = False
        mock_config.doAddSky = False
        mock_config.doAddSource = False
        mock_config.doRoundAdu = False
        mock_config.doAddHighSignalNonlinearity = False
        mock_config.doApplyGain = False
        mock_config.doAddCrosstalk = False
        mock_config.doAddBrightDefects = False
        mock_config.doAddParallelOverscanRamp = False
        mock_config.doAddSerialOverscanRamp = False

        mock_config.isTrimmed = True
        mock_config.doGenerateImage = True

        return mock_config

    def get_isr_config_minimal_corrections(self):
        """Get an IsrTaskLSSTConfig with minimal corrections.
        """
        isr_config = IsrTaskLSSTConfig()
        isr_config.doBias = False
        isr_config.doDark = False
        isr_config.doDeferredCharge = False
        isr_config.doLinearize = False
        isr_config.doCorrectGains = False
        isr_config.doCrosstalk = False
        isr_config.doDefect = False
        isr_config.doBrighterFatter = False
        isr_config.doFlat = False
        # We override the leading/trailing to skip here because of the limited
        # size of the test camera overscan regions.
        defaultAmpConfig = isr_config.overscanCamera.getOverscanDetectorConfig(self.detector).defaultAmpConfig
        defaultAmpConfig.doSerialOverscan = True
        defaultAmpConfig.serialOverscanConfig.leadingToSkip = 0
        defaultAmpConfig.serialOverscanConfig.trailingToSkip = 0
        defaultAmpConfig.doParallelOverscanCrosstalk = False
        defaultAmpConfig.doParallelOverscan = True
        defaultAmpConfig.parallelOverscanConfig.leadingToSkip = 0
        defaultAmpConfig.parallelOverscanConfig.trailingToSkip = 0
        # Our strong overscan slope in the tests requires an override.
        defaultAmpConfig.parallelOverscanConfig.maxDeviation = 300.0
        # Override the camera model to use the desired saturation (ADU).
        defaultAmpConfig.saturation = self.saturation_adu  # ADU

        isr_config.doAssembleCcd = True

        return isr_config

    def get_isr_config_electronic_corrections(self):
        """Get an IsrTaskLSSTConfig with electronic corrections.

        This tests all the corrections that we support in the mocks/ISR.
        """
        isr_config = IsrTaskLSSTConfig()
        # We add these as appropriate in the tests.
        isr_config.doBias = False
        isr_config.doDark = False
        isr_config.doFlat = False

        # These are the electronic effects the tests support (in addition
        # to overscan).
        isr_config.doCrosstalk = True
        isr_config.doDefect = True
        isr_config.doLinearize = True

        # This is False because it is only used in a single test case
        # as it takes a while to solve
        isr_config.doBrighterFatter = False

        # These are the electronic effects we do not support in tests yet.
        isr_config.doDeferredCharge = False
        isr_config.doCorrectGains = False

        # We override the leading/trailing to skip here because of the limited
        # size of the test camera overscan regions.
        defaultAmpConfig = isr_config.overscanCamera.getOverscanDetectorConfig(self.detector).defaultAmpConfig
        defaultAmpConfig.doSerialOverscan = True
        defaultAmpConfig.serialOverscanConfig.leadingToSkip = 0
        defaultAmpConfig.serialOverscanConfig.trailingToSkip = 0
        defaultAmpConfig.doParallelOverscanCrosstalk = True
        defaultAmpConfig.doParallelOverscan = True
        defaultAmpConfig.parallelOverscanConfig.leadingToSkip = 0
        defaultAmpConfig.parallelOverscanConfig.trailingToSkip = 0
        # Our strong overscan slope in the tests requires an override.
        defaultAmpConfig.parallelOverscanConfig.maxDeviation = 300.0
        # Override the camera model to use the desired saturation (ADU).
        defaultAmpConfig.saturation = self.saturation_adu  # ADU

        isr_config.doAssembleCcd = True

        return isr_config

    def get_non_defect_pixels(self, mask_origin):
        """Get the non-defect pixels to compare.

        Parameters
        ----------
        mask_origin : `lsst.afw.image.MaskX`
            The origin mask (for shape and type).

        Returns
        -------
        pix_x, pix_y : `tuple` [`np.ndarray`]
            x and y values of good pixels.
        """
        mask_temp = mask_origin.clone()
        mask_temp[:, :] = 0

        for defect in self.defects:
            mask_temp[defect.getBBox()] = 1

        return np.where(mask_temp.array == 0)

    def _check_bad_column_crosstalk_correction(
        self,
        exp,
        nsigma_cut=5.0,
    ):
        """Test bad column crosstalk correction.

        This includes possible provblems from parallel overscan
        crosstalk and gain mismatches.

        The target amp is self.detector[0], "C:0,0".

        Parameters
        ----------
        exp : `lsst.afw.image.Exposure`
            Input exposure.
        nsigma_cut : `float`, optional
            Number of sigma to check for outliers.
        """
        amp = self.detector[0]
        amp_image = exp[amp.getBBox()].image.array
        sigma = median_abs_deviation(amp_image.ravel(), scale="normal")

        med = np.median(amp_image.ravel())
        self.assertLess(amp_image.max(), med + nsigma_cut*sigma)
        self.assertGreater(amp_image.min(), med - nsigma_cut*sigma)


class MemoryTester(lsst.utils.tests.MemoryTestCase):
    pass


def setup_module(module):
    lsst.utils.tests.init()


if __name__ == "__main__":
    lsst.utils.tests.init()
    unittest.main()