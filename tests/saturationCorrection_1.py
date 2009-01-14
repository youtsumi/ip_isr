"""
@brief A simple test for the ISR stage, 'Saturation Correction'.

@author Nicole M. Silvestri,
        University of Washington
        nms@astro.washington.edu

file created Nov 20, 2008

@file
"""

import os
import math
import pdb  # we may want to say pdb.set_trace()
import unittest

import numpy

import eups

import lsst.afw.image as afwImage
import lsst.afw.math as afwMath
import lsst.utils.tests as utilsTests
import lsst.afw.image.testUtils as imUtilsTests
import lsst.pex.logging as pexLog
import lsst.pex.exceptions as pexEx
import lsst.pex.policy as pexPolicy
import lsst.ip.isr.SaturationCorrection as ipIsrSat

Verbosity = 4 # increase from zero to see trace
pexLog.Trace_setVerbosity("lsst.ip.isr", Verbosity)

dataDir = eups.productDir("afwdata")
if not dataDir:
    raise RuntimeError("Must set up afwdata to run these tests!")

isrDir = eups.productDir("ip_isr")
if not isrDir:
    raise RuntimeError("Must set up ip_isr to run these tests!")

## INPUT IMAGE AND PATH NAMES

inFilePath = os.path.join(dataDir, "CFHT", "D4", "raw-53535-i-797722_1")
isrPolicyPath = os.path.join(isrDir, "pipeline", "isrPolicy.paf")
satLookupTablePath = (isrDir, "pipeline", "satLookUpTable")

## OUTPUT IMAGE AND PATH NAMES

outputPath = os.path.join(dataDir, "testSatCorExposure")

#-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-

class isrTestCases(unittest.TestCase):
    """
    Tests for the ISR stage, 'Saturation Correction'.
    """
    def setUp(self):
        self.chunkExposure = afwImage.ExposureF()
        self.chunkExposure.readFits(inFilePath)
        self.isrPolicy = pexPolicy.Policy.createPolicy(isrPolicyPath)
        
    def tearDown(self):
        del self.chunkExposure
        del self.isrPolicy

    def testSaturationCorrection(self):

        chunkMaskedImage = self.chunkExposure.getMaskedImage()
        numCols = chunkMaskedImage.getCols()
        numRows = chunkMaskedImage.getRows()
        numpixels = numCols * numRows
        
        lookupTable = open(lookupTablePath, "rU")  
        pixelValues = lookupTable.readlines()
        numPix = len(pixelValues)
        print 'Number of pixels: ', numPix
        for pixels in pixelValues:
            # strip trailing whitespace, returns, etc.
            pixels = pixels.strip()
            # ignore blank lines
            if not pixels:
                continue
            # ignore comment lines
            if pixels.startswith("#"):
                continue
            lookupList = pixels.split()
            if len(pixelList) < numPixels or len(pixelList) > numPixels:
                print "Cannot parse: " pixels
      
        ipIsrSat.saturationCorrection(self.chunkExposure, self.isrPolicy, lookupList)

        
        
        self.chunkExposure.writeFits(outputPath)    
        
#-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-

def suite():
    """
    Returns a suite containing all the test cases in this module.
    """
    utilsTests.init()

    suites = []
    suites += unittest.makeSuite(isrTestCases)
    suites += unittest.makeSuite(utilsTests.MemoryTestCase)

    return unittest.TestSuite(suites)

def run(exit=False):
    """Run the tests"""
    utilsTests.run(suite(), exit)
	   
if __name__ == "__main__":
    #   utilsTests.run(suite())
    run(True)
