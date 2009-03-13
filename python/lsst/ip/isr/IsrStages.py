import time, re, os, math
import eups
import lsst.afw.detection   as afwDetection
import lsst.afw.image       as afwImage
import lsst.afw.math        as afwMath
import lsst.meas.algorithms as algorithms
import lsst.pex.logging     as pexLog
import lsst.pex.exceptions  as pexExcept
import lsst.meas.algorithms as measAlgorithms
# relative imports
import isrLib

#
### Actual Pipeline stage to run the ISR
#
from lsst.pex.harness.Stage import Stage

class IsrStage(Stage):

    def process(self):
        self.activeClipboard = self.inputQueue.getNextDataset()

        inputImageName       = self._policy.get('inputImageName')
        inputMetadataName    = self._policy.get('inputMetadataName')

        inputImage           = self.activeClipboard.get(inputImageName)
        inputMetadata        = self.activeClipboard.get(inputMetadata)
        calibData            = self.activeClipboard.get('calibData')

        # Grab the necessary calibration data products
        biasPath             = calibData['bias']
        darkPath             = calibData['dark']
        defectPath           = calibData['defect']
        flatPath             = calibData['flat']
        fringePath           = calibData['fringe']
        linearizePath        = calibData['linearize']
        
        # Step 1 : create an exposure
        inputExposure = ExposureFromInputData(inputImage, inputMetadata)

        ###
        # Isr Substages
        #
        
        # Linearize
        linearityTable  = LookupTableFromPolicy(linearizePath)
        Linearization(inputExposure, self._policy, linearityTable)

        # Overscan correction
        OverscanCorrection(inputExposure, self._policy)
        
        # Trim; yields new exposure
        calibratedExposure = TrimNew(inputExposure, self._policy)

        # Saturation correction
        SaturationCorrection(calibratedExposure, self._policy)

        # Bias correct
        bias = imageFromInputData(biasPath)
        BiasCorrection(calibratedExposure, bias, self._policy)

        # Dark correct
        dark = imageFromInputData(darkPath)
        DarkCorrection(calibratedExposure, dark, self._policy)

        # Flat field
        flat = imageFromInputData(flatPath)
        FlatCorrection(calibratedExposure, flat, self._policy)

        # Fringe; not for DC3a
        fringe = imageFromInputData(fringePath)
        
        # Finally, mask bad pixels
        defectList = measAlgorithms.policyToBadRegionList(defectPath)
        MaskBadPixelsDef(calibratedExposure, self._policy, defectList)

        # And cosmic rays
        CrRejection(calibratedExposure, self._policy)
            
        #
        # Isr Substages
        ###

        self.activeClipboard.put('calibratedExposure', calibratedExposure)
        self.outputQueue.addDataset(self.activeClipboard)

        

    

#
### STAGE : Assemble Exposure from input Image
#


# ISSUE - HOW DO WE GET THE HEADER INFO OF THE CALIBRATION DATA?????

def ExposureFromInputData(image, metadata, makeWcs=True):
    # makeMaskedImage() will make a MaskedImage with the same type as Image
    mi   = afwImage.makeMaskedImage(image)

    # Generate an empty mask
    mask = afwImage.Mask(mi.getDimensions())
    mi.setMask(mask)

    # Generate a variance from the image pixels and gain
    variance  = image.Factory(image, True)
    gain      = metadata.get('gain')
    variance /= gain
    mi.setVariance(variance)

    if makeWcs:
        # Extract the Wcs info from the input metadata
        wcs      = afwImage.Wcs(metadata)
    else:
        wcs      = afwImage.Wcs()
        
    # makeExposure will make an Exposure with the same type as MaskedImage
    exposure = afwImage.makeExposure(mi, wcs)
    
    return exposure

#
### STAGE : Validation of the image sizes, contents, etc.
#

def ValidateCalibration(exposure, calibration, policy):
    """
    Make sure that the images are the same size, were derived from
    the same chunk of the focal plane, etc

    Things to check are :
     * Image Size (all)
     * From the same piece of the focal plane (illum, flat)
     * From the same piece of silicon (bad pixel mask, bias)
     * Through the same filter (dome flat)
     * Appropriate for the date range (anything time variable; dflats, etc)
    """
    
    pass

#
### STAGE : Bad pixel correction
#

def MaskFromDefects(dimensions, fpList):
    # the output LSST Mask image
    mask    = afwImage.MaskU(dimensions)
    mask.set(0)
    bitmask = mask.getPlaneBitMask('BAD')

    # set the bits
    afwDetection.setMaskFromFootprintList(mask, fpList, bitmask)

    return mask
    

def DefectsFromCfhtImage(fitsfile):
    # input bad pixel image
    image   = afwImage.ImageF(fitsfile)
    image  *= -1

    # turn into masked image for detection
    mi = afwImage.MaskedImageF(image)

    # find bad regions
    thresh    = afwDetection.Threshold(-0.5)
    ds        = afwDetection.DetectionSetF(mi, thresh)
    fpList    = ds.getFootprints()

    return fpList


def MaskBadPixelsFp(exposure, policy, fpList,
                    interpolate = True,
                    maskName    = 'BAD',
                    stageSig    = isrLib.ISR_BADP,
                    stageName   = 'lsst.ip.isr.maskbadpixels'):
                  
    # common input test
    metadata   = exposure.getMetadata()
    if metadata.exists(stageSig):
        pexLog.Trace(stageName, 4, '%s has already been run' % (stageSig))
        return

    # mask bad pixels
    mi      = exposure.getMaskedImage()
    mask    = mi.getMask()
    bitmask = mask.getPlaneBitMask(maskName)
    afwDetection.setMaskFromFootprintList(mask, fpList, bitmask)    

    if interpolate:
        # and interpolate over them
        defaultFwhm = policy.getDouble('defaultFwhm')
        psf = algorithms.createPSF('DoubleGaussian', 0, 0, defaultFwhm/(2*math.sqrt(2*math.log(2))))
        for fp in fpList:
            defect = afwDetection.Defect(fp.getBBox())
            algorithms.interpolateOverDefects(mi, psf, defect)

        stageSummary = 'with interpolation'
    else:
        stageSummary = 'without interpolation'

        
    # common outputs
    pexLog.Trace(stageName, 4, '%s %s' % (stageSig, stageSummary))    
    metadata.setString(stageSig, '%s; %s' % (stageSummary, time.asctime()))


def MaskBadPixelsDef(exposure, policy, defectList,
                     interpolate = True,
                     maskName    = 'BAD',
                     stageSig    = isrLib.ISR_BADP,
                     stageName   = 'lsst.ip.isr.maskbadpixels'):
                  
    # common input test
    metadata   = exposure.getMetadata()
    if metadata.exists(stageSig):
        pexLog.Trace(stageName, 4, '%s has already been run' % (stageSig))
        return

    # mask bad pixels
    mi      = exposure.getMaskedImage()
    mask    = mi.getMask()
    bitmask = mask.getPlaneBitMask(maskName)
    for defect in defectList:
        afwDetection.setMaskFromFootprint(mask, defect.getFootprint(), bitmask)    

    if interpolate:
        # and interpolate over them
        defaultFwhm = policy.getDouble('defaultFwhm')
        psf = algorithms.createPSF('DoubleGaussian', 0, 0, defaultFwhm/(2*math.sqrt(2*math.log(2))))
        for fp in fpList:
            defect = afwDetection.Defect(fp.getBBox())
            algorithms.interpolateOverDefects(mi, psf, defect)

        stageSummary = 'with interpolation'
    else:
        stageSummary = 'without interpolation'

        
    # common outputs
    pexLog.Trace(stageName, 4, '%s %s' % (stageSig, stageSummary))    
    metadata.setString(stageSig, '%s; %s' % (stageSummary, time.asctime()))

#
### STAGE : Linearization
#

def LookupTableFromPolicy(tablePolicy,
                          stageName = 'lsst.ip.isr.lookuptablefrompolicy'):
    tableType   = tablePolicy.getString('type')
    tableLength = tablePolicy.getInt('length')
    tableValues = tablePolicy.getArray('value')
    assert len(tableValues) == tableLength
    tableValues = afwMath.vectorD(tableValues)

    if tableType == 'Replace':
        lookupTable = isrLib.LookupTableReplaceI(tableValues)
    elif tableType == 'Multiplicative':
        lookupTable = isrLib.LookupTableMultiplicativeF(tableValues)
    else:
        pexLog.Trace(stageName, 4, 'Unknown table type : %s' % (tableType))
        return None
    
    return lookupTable


def Linearization(exposure, policy,
                  lookupTable = None,
                  stageSig    = isrLib.ISR_LIN,
                  stageName   = 'lsst.ip.isr.linearization',
                  policyPath  = os.path.join(eups.productDir('ip_isr'), 'pipeline')):

    # common input test
    metadata   = exposure.getMetadata()
    if metadata.exists(stageSig):
        pexLog.Trace(stageName, 4, '%s has already been run' % (stageSig))
        return

    if lookupTable == None:
        lookupTableName = policy.getPolicy('linearizePolicy').getString('lookupTableName')
        lookupTable     = LookupTableFromPolicy(os.path.join(policyPath, lookupTableName))
    else:
        lookupTableName = 'provided to ipIsr.Linearization'

    mi = exposure.getMaskedImage()
    lookupTable.apply(mi)
    
    # common outputs
    stageSummary = 'using table %s' % (lookupTableName)
    pexLog.Trace(stageName, 4, '%s %s' % (stageSig, stageSummary))    
    metadata.setString(stageSig, '%s; %s' % (stageSummary, time.asctime()))

#
### STAGE : Cosmic Ray Rejection
#

def CrRejection(exposure, policy,
                stageSig      = isrLib.ISR_CRREJ,
                stageName     = 'lsst.ip.isr.crreject',
                subBackground = True):
    
    # common input test
    metadata   = exposure.getMetadata()
    if metadata.exists(stageSig):
        pexLog.Trace(stageName, 4, '%s has already been run' % (stageSig))
        return

    crPolicy    = policy.getPolicy('crRejectionPolicy')
    gainKeyword = crPolicy.getString('gainKeyword')
    gain        = metadata.getDouble(gainKeyword)
    # needed for CR
    crPolicy.set('e_per_dn', gain)

    mi = exposure.getMaskedImage()
    if subBackground:
        # how much of this do we put in policy?
        bctrl = afwMath.BackgroundControl(afwMath.NATURAL_SPLINE)
        bctrl.setNxSample(max(2, int(mi.getWidth()/256) + 1))
        bctrl.setNySample(max(2, int(mi.getHeight()/256) + 1))
        bctrl.sctrl.setNumSigmaClip(3)
        bctrl.sctrl.setNumIter(3)
        
        im      = mi.getImage()
        backobj = afwMath.makeBackground(im, bctrl)
        im     -= backobj.getImageF()

    # NOTE - this background issue needs to be resolved
    bg = 0.
    
    defaultFwhm = policy.getDouble('defaultFwhm')
    psf         = algorithms.createPSF('DoubleGaussian', 0, 0, defaultFwhm/(2*math.sqrt(2*math.log(2))))
    crs         = algorithms.findCosmicRays(mi, psf, bg, crPolicy, False)    
    
    if subBackground:
        im     += backobj.getImageF() 
    
    # common outputs
    stageSummary = 'with background subtraction = %s; found %d CRs' % (str(subBackground),
                                                                       len(crs))
    pexLog.Trace(stageName, 4, '%s %s' % (stageSig, stageSummary))    
    metadata.setString(stageSig, '%s; %s' % (stageSummary, time.asctime()))

#
### STAGE : Saturation correction
#

def SaturationCorrection(exposure, policy,
                         interpolate = True,
                         maskName    = 'SAT',
                         stageSig    = isrLib.ISR_SAT,
                         stageName   = 'lsst.ip.isr.saturationcorrection'):

    # common input test
    metadata   = exposure.getMetadata()
    if metadata.exists(stageSig):
        pexLog.Trace(stageName, 4, '%s has already been run' % (stageSig))
        return

    try:
        satKeyword = policy.getPolicy('saturationPolicy').getString('saturationKeyword')
        saturation = metadata.getDouble(satKeyword)
    except:
        saturation = policy.getPolicy('saturationPolicy').getDouble('defaultSaturation')
        pexLog.Trace(stageName, 4, 'Unable to read %s, using default saturation of %s' % (satKeyword, saturation))    
        
    mi         = exposure.getMaskedImage()
    mask       = mi.getMask()
    bitmask    = mask.getPlaneBitMask(maskName)

    # find saturated regions
    thresh     = afwDetection.Threshold(saturation)
    ds         = afwDetection.DetectionSetF(mi, thresh)
    fpList     = ds.getFootprints()
    # we will turn them into defects for interpolating
    defectList = algorithms.DefectListT()
    
    # grow them
    growSaturated = policy.getPolicy('saturationPolicy').getInt('growSaturated')
    for fp in fpList:
        # if "True", growing requires a convolution
        # if "False", its faster
        fpGrow = afwDetection.growFootprint(fp, growSaturated, False)
        afwDetection.setMaskFromFootprint(mask, fpGrow, bitmask)

        if interpolate:
            defect = algorithms.Defect(fpGrow.getBBox())
            defectList.push_back(defect)

    # interpolate over them
    if interpolate:
        mask.addMaskPlane('INTERP')
        defaultFwhm   = policy.getDouble('defaultFwhm')
        psf = algorithms.createPSF('DoubleGaussian', 0, 0, defaultFwhm/(2*math.sqrt(2*math.log(2))))
        algorithms.interpolateOverDefects(mi, psf, defectList)
    
    # common outputs
    stageSummary = 'using %s=%.2f' % (satKeyword, saturation)
    pexLog.Trace(stageName, 4, '%s %s' % (stageSig, stageSummary))    
    metadata.setString(stageSig, '%s; %s' % (stageSummary, time.asctime()))

#
### STAGE : Bias / dark correction
#

def BiasCorrection(exposure, bias, policy,
                   stageSig  = isrLib.ISR_BIAS,
                   stageName = 'lsst.ip.isr.biascorrection'):

    # common input test
    metadata   = exposure.getMetadata()
    if metadata.exists(stageSig):
        pexLog.Trace(stageName, 4, '%s has already been run' % (stageSig))
        return

    bmetadata         = bias.getMetadata()
    
    filenameKeyword   = policy.getString('filenameKeyword')
    filename          = bmetadata.getString(filenameKeyword)

    meanCountsKeyword = policy.getPolicy('biasPolicy').getString('meanCountsKeyword')
    meanCounts        = bmetadata.getDouble(meanCountsKeyword)

    mi  = exposure.getMaskedImage()
    bmi = bias.getMaskedImage()
    mi -= bmi

    # common outputs
    stageSummary = 'using %s with mean=%.2f' % (filename, meanCounts)
    pexLog.Trace(stageName, 4, '%s %s' % (stageSig, stageSummary))    
    metadata.setString(stageSig, '%s; %s' % (stageSummary, time.asctime()))
    

def DarkCorrection(exposure, dark, policy,
                   stageSig  = isrLib.ISR_DARK,
                   stageName = 'lsst.ip.isr.darkcorrection'):
    
    # common input test
    metadata   = exposure.getMetadata()
    if metadata.exists(stageSig):
        pexLog.Trace(stageName, 4, '%s has already been run' % (stageSig))
        return

    dmetadata         = dark.getMetadata()
    filenameKeyword   = policy.getString('filenameKeyword')
    filename          = dmetadata.getString(filenameKeyword)

    scalingKeyword    = policy.getPolicy('darkPolicy').getString('darkScaleKeyword') # e.g. EXPTIME
    expscaling        = metadata.getDouble(scalingKeyword)
    darkscaling       = dmetadata.getDouble(scalingKeyword)
    scale             = expscaling / darkscaling

    mi  = exposure.getMaskedImage()
    mi.scaledMinus(scale, dark.getMaskedImage())

    # common outputs
    stageSummary = 'using %s with scale=%.2f' % (filename, scale)
    pexLog.Trace(stageName, 4, '%s %s' % (stageSig, stageSummary))    
    metadata.setString(stageSig, '%s; %s' % (stageSummary, time.asctime()))
    
                   
#
### STAGE : Flat / illum correction
#

def FlatCorrection(exposure, flat, policy,
                   stageSig  = isrLib.ISR_DFLAT,
                   stageName = 'lsst.ip.isr.flatcorrection'):

    # common input test
    metadata   = exposure.getMetadata()
    if metadata.exists(stageSig):
        pexLog.Trace(stageName, 4, '%s has already been run' % (stageSig))
        return

    fmetadata         = flat.getMetadata()
    filenameKeyword   = policy.getString('filenameKeyword')
    filename          = fmetadata.getString(filenameKeyword)

    scalingKeyword    = policy.getPolicy('flatPolicy').getString('flatScaleKeyword') # e.g. MEAN
    flatscaling       = fmetadata.getDouble(scalingKeyword)

    mi   = exposure.getMaskedImage()
    mi.scaledDivides(1./flatscaling, flat.getMaskedImage())
    
    # common outputs
    stageSummary = 'using %s with scale=%.2f' % (filename, flatscaling)
    pexLog.Trace(stageName, 4, '%s %s' % (stageSig, stageSummary))    
    metadata.setString(stageSig, '%s; %s' % (stageSummary, time.asctime()))


def IlluminationCorrection(exposure, illum, policy,
                           stageSig  = isrLib.ISR_ILLUM,
                           stageName = 'lsst.ip.isr.illuminationcorrection'):

    # common input test
    metadata   = exposure.getMetadata()
    if metadata.exists(stageSig):
        pexLog.Trace(stageName, 4, '%s has already been run' % (stageSig))
        return

    imetadata         = illum.getMetadata()
    filenameKeyword   = policy.getString('filenameKeyword')
    filename          = imetadata.getString(filenameKeyword)

    scalingKeyword    = policy.getPolicy('illuminationPolicy').getString('illumScaleKeyword')
    illumscaling      = imetadata.getDouble(scalingKeyword)

    mi   = exposure.getMaskedImage()
    mi.scaledDivides(1./illumscaling, illum.getMaskedImage())
    
    # common outputs
    stageSummary = 'using %s with scale=%.2f' % (filename, illumscaling)
    pexLog.Trace(stageName, 4, '%s %s' % (stageSig, stageSummary))    
    metadata.setString(stageSig, '%s; %s' % (stageSummary, time.asctime()))


#
### STAGE : Trim / overscan correction
#

# Now implemented in C++
#
#def BBoxFromDatasec(string,
#                    stageName = 'lsst.ip.isr.bboxfromdatasec'):
#    
#    c = re.compile('^\[(\d+):(\d+),(\d+):(\d+)\]$')
#    m = c.match(string)
#    if m:
#        startCol, endCol, startRow, endRow = m.groups()
#        # Beware the FITS convention
#        startCol -= floor((1 + 0.5 - afwImage.PixelZeroPos))
#        startRow -= floor((1 + 0.5 - afwImage.PixelZeroPos))
#    else:
#        raise pexExcept.LsstException, '%s : Cannot parse %s' % (stageName, string)
#
#    bbox = afwImage.BBox(afwImage.PointI(startCol, startRow),
#                         endCol-startCol,
#                         endRow-startRow)
#    return bbox

def TrimNew(exposure, policy,
            stageSig  = isrLib.ISR_TRIM,
            stageName = 'lsst.ip.isr.trim'):
    """
    This returns a new Exposure that is a subsection of the input exposure.
    
    NOTE : do we need to deal with the WCS in any way, shape, or form?
    """
    
    # common input test
    metadata   = exposure.getMetadata()
    if metadata.exists(stageSig):
        pexLog.Trace(stageName, 4, '%s has already been run' % (stageSig))
        return

    trimsecKeyword  = policy.getPolicy('trimPolicy').getString('trimsecKeyword')
    trimsec         = metadata.getString(trimsecKeyword)
    trimsecBBox     = isrLib.BBoxFromDatasec(trimsec)

    # if "True", do a deep copy
    trimmedExposure = afwImage.ExposureF(exposure, trimsecBBox, False)
    #llc = trimsecBBox.getLLC()
    #trimmedExposure.setXY0(llc)

    # common outputs
    stageSummary = 'using trimsec %s' % (trimsec)
    pexLog.Trace(stageName, 4, '%s %s' % (stageSig, stageSummary))    
    metadata.setString(stageSig, '%s; %s' % (stageSummary, time.asctime()))

    return trimmedExposure


def OverscanCorrection(exposure, policy,
                       stageSig  = isrLib.ISR_OSCAN,
                       stageName = 'lsst.ip.isr.overscancorrection'):
    """
    This returns a new Exposure that is a subsection of the input exposure.
    
    NOTE : do we need to deal with the WCS in any way, shape, or form?
    """

    # common input test
    metadata   = exposure.getMetadata()
    if metadata.exists(stageSig):
        pexLog.Trace(stageName, 4, '%s has already been run' % (stageSig))
        return

    mi = exposure.getMaskedImage()
    
    overscanKeyword = policy.getPolicy('overscanPolicy').getString('overscanKeyword')
    overscan        = metadata.getString(overscanKeyword)
    overscanBBox    = isrLib.BBoxFromDatasec(overscan)

    # if "True", do a deep copy
    overscanData    = afwImage.ImageF(exposure.getMaskedImage().getImage(), overscanBBox, False)

    # what type of overscan modeling?
    overscanFitType = policy.getPolicy('overscanPolicy').getString('overscanFitType')
    if overscanFitType == 'MEAN':
        offset = afwMath.makeStatistics(overscanData, afwMath.MEAN).getValue(afwMath.MEAN)
        mi    -= offset
    elif overscanFitType == 'MEDIAN':
        offset = afwMath.makeStatistics(overscanData, afwMath.MEDIAN).getValue(afwMath.MEDIAN)
        mi    -= offset
    elif overscanFitType == 'POLY':
        polyOrder = policy.getPolicy('overscanPolicy').getInt('polyOrder')
        raise pexExcept.LsstException, '%s : %s not implemented' % (stageName, overscanFitType)
    else:
        raise pexExcept.LsstException, '%s : %s an invalid overscan type' % (stageName, overscanFitType)

    # common outputs
    stageSummary = 'using overscan section %s with %s=%f' % (overscan, overscanFitType, offset)
    pexLog.Trace(stageName, 4, '%s %s' % (stageSig, stageSummary))    
    metadata.setString(stageSig, '%s; %s' % (stageSummary, time.asctime()))

#
### STAGE : Fringe correction
#

def FringeCorrection(exposure, fringe, policy,
                     stageSig  = isrLib.ISR_FRING,
                     stageName = 'lsst.ip.isr.fringecorrection'):

    fringeSkyKeyword   = policy.getPolicy('fringePolicy').getString('fringeSkyKeyword')
    fringeScaleKeyword = policy.getPolicy('fringePolicy').getString('fringeScaleKeyword')
    
    raise pexExcept.LsstException, '%s not implemented' % (stageName)

#
### STAGE : Pupil correction
#

def PupilCorrection(exposure, fringe, policy,
                    stageSig  = isrLib.ISR_PUPIL,
                    stageName = 'lsst.ip.isr.pupilcorrection'):

    raise pexExcept.LsstException, '%s not implemented' % (stageName)
