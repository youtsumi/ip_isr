import time, os, math
import lsst.utils           as utils
import lsst.afw.detection   as afwDetection
import lsst.afw.image       as afwImage
import lsst.afw.math        as afwMath
import lsst.meas.algorithms as algorithms
import lsst.pex.logging     as pexLog
import lsst.pex.policy      as pexPolicy
import lsst.pex.exceptions  as pexExcept

# relative imports
import isrLib

def calculateSdqaRatings(exposure):
    metrics = {}
    '''
    mi      = exposure.getMaskedImage()
    mask    = mi.getMask()
    badbitmask = mask.getPlaneBitMask('BAD')
    satbitmask = mask.getPlaneBitMask('SAT')
    intrpbitmask = mask.getPlaneBitMask('INTRP')
    #Assuming this means all pixels marked bad
    nBadCalibPix
    nSaturatePix
    overscanMean
    overscanStdDev
    overscanMedian
    imageMedian
    imageClippedMean  #
    imageMin
    imageMax
    imageGradientX
    imageGradientY
    amplFringe = None
    '''

    return metrics

def exposureFromInputData(image, metadata, ampBBox,
                          makeWcs     = True,
                          policy      = None,
                          defaultGain = 1.0,
                          imageSource = afwImage.ImageF):
    methodName = "isr.exposureFromInputData"

    # Generate an empty mask
    mask = afwImage.MaskU(image.getDimensions())
    mask.set(0)

    # Generate a variance from the image pixels and gain
    #var  = afwImage.ImageF(image, True)
    var = afwImage.ImageF(image)
    
    if metadata.exists('gain'):
        gain = metadata.get('gain')
    elif policy:
        filenameKeyword = policy.get('filenameKeyword')
        filename        = metadata.get(filenameKeyword)
        if policy.exists('defaultGainKeyword'):
            gainKeyword = policy.get('defaultGainKeyword')
            if metadata.exists(gainKeyword):
                gain = metadata.get(gainKeyword)
            else:
                pexLog.Trace(methodName, 4, 'Using default gain=%f for %s' % (defaultGain, filename))
                gain = defaultGain
        else:
            pexLog.Trace(methodName, 4, 'Using default gain=%f for %s' % (defaultGain, filename))
            gain = defaultGain
    else:
        pexLog.Trace(methodName, 4, 'Using default gain=%f' % (defaultGain))
        gain = defaultGain
    # Normalize by the gain
    var /= gain

    # makeMaskedImage() will make a MaskedImage with the same type as Image
    mi   = afwImage.makeMaskedImage(image, mask, var)
    mi.setXY0(ampBBox.getX0(), ampBBox.getY0())

    if makeWcs:
        # Extract the Wcs info from the input metadata
        wcs      = afwImage.makeWcs(metadata)
    else:
        wcs      = afwImage.Wcs()
        
    # makeExposure will make an Exposure with the same type as MaskedImage
    exposure = afwImage.makeExposure(mi, wcs)
    exposure.setMetadata(metadata)

    return exposure


def validateCalibration(exposure, calibration, policy):
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


def maskFromDefects(dimensions, fpList):
    # the output LSST Mask image
    mask = afwImage.MaskU(dimensions)
    mask.set(0)
    bitmask = mask.getPlaneBitMask('BAD')

    # set the bits
    afwDetection.setMaskFromFootprintList(mask, fpList, bitmask)

    return mask
    

def defectsFromBoolImage(fitsfile, invert=False):
    # input bad pixel image
    # This assumes an image with ones and zeros
    image = afwImage.ImageF(fitsfile)
    if invert:
        image *= -1
        thresh = afwDetection.Threshold(-0.5)
    else:
        thresh = afwDetection.Threshold(0.5)
   
    # turn into masked image for detection
    mi = afwImage.MaskedImageF(image)

    # find bad regions
    ds = afwDetection.FootprintSetF(mi, thresh)
    fpList = ds.getFootprints()

    return fpList


def maskBadPixelsFp(exposure, policy, fpList,
                    interpolate = True,
                    maskName    = 'BAD'):
                  
    raise RuntimeError, "Do not call me; use MaskBadPixelsDef.  Talk to RHL if you disagree"

    # common input test
    metadata   = exposure.getMetadata()

    # mask bad pixels
    mi      = exposure.getMaskedImage()
    mask    = mi.getMask()
    bitmask = mask.getPlaneBitMask(maskName)
    afwDetection.setMaskFromFootprintList(mask, fpList, bitmask)

    if interpolate:
        # and interpolate over them
        defaultFwhm = policy.get('defaultFwhm')
        psf = algorithms.createPSF('DoubleGaussian', 0, 0, defaultFwhm/(2*math.sqrt(2*math.log(2))))
        for fp in fpList:
            defect = afwDetection.Defect(fp.getBBox())
            algorithms.interpolateOverDefects(mi, psf, defect)

        


def maskBadPixelsDef(exposure, defectList, fwhm,
                     interpolate = True,
                     maskName    = 'BAD'):
                  
    # mask bad pixels
    mi      = exposure.getMaskedImage()
    mask    = mi.getMask()
    bitmask = mask.getPlaneBitMask(maskName)
    
    for defect in defectList:
        bbox = defect.getBBox()
        afwDetection.setMaskFromFootprint(mask, afwDetection.Footprint(bbox), bitmask)    

    if interpolate:
        # and interpolate over them
        psf = algorithms.createPSF('DoubleGaussian', 0, 0, fwhm/(2*math.sqrt(2*math.log(2))))
        fallbackValue = afwMath.makeStatistics(mi.getImage(), afwMath.MEANCLIP).getValue()
        algorithms.interpolateOverDefects(mi, psf, defectList, fallbackValue)

        

def lookupTableFromPolicy(tablePolicy):
    tableType   = tablePolicy.getString('type')
    tableLength = tablePolicy.getInt('length')
    tableValues = tablePolicy.getArray('value')
    assert len(tableValues) == tableLength
    tableValues = afwMath.vectorD(tableValues)

    if tableType == 'Replace':
        lookupTable = isrLib.LookupTableReplaceF(tableValues)
    elif tableType == 'Multiplicative':
        lookupTable = isrLib.LookupTableMultiplicativeF(tableValues)
    else:
        return None
    
    return lookupTable


def linearization(exposure, lookupTable):

    # common input test
    metadata   = exposure.getMetadata()
    gain = metadata.get('gain')
    mi   = exposure.getMaskedImage()
    lookupTable.apply(mi, gain)
    
#Get rid of this: SIMON
def backgroundSubtraction(exposure, gridsize="32",
        interptype="AKIMA_SPLINE", nsigma=3.0, niter=3.0):

    metadata = exposure.getMetadata()
    mi = exposure.getMaskedImage()
    bctrl = None
    try:
        bctrl = {
            'LINEAR'               :
            afwMath.BackgroundControl(afwMath.Interpolate.LINEAR), 
            'NATURAL_SPLINE'       :
            afwMath.BackgroundControl(afwMath.Interpolate.NATURAL_SPLINE), 
            'CUBIC_SPLINE'         :
            afwMath.BackgroundControl(afwMath.Interpolate.CUBIC_SPLINE), 
            'CUBIC_SPLINE_PERIODIC':
            afwMath.BackgroundControl(afwMath.Interpolate.CUBIC_SPLINE_PERIODIC), 
            'AKIMA_SPLINE'         :
            afwMath.BackgroundControl(afwMath.Interpolate.AKIMA_SPLINE), 
            'AKIMA_SPLINE_PERIODIC':
            afwMath.BackgroundControl(afwMath.Interpolate.CUBIC_SPLINE_PERIODIC) 
        }[interptype]
    except:
        bctrl = afwMath.BackgroundControl(afwMath.Interpolate.AKIMA_SPLINE)
    bctrl.setNxSample(max(2, int(mi.getWidth()/gridsize) + 1))
    bctrl.setNySample(max(2, int(mi.getHeight()/gridsize) + 1))
    bctrl.sctrl.setNumSigmaClip(nsigma)
    bctrl.sctrl.setNumIter(niter)
    im      = mi.getImage()
    backobj = afwMath.makeBackground(im, bctrl)
    im     -= backobj.getImageF()

    
def crRejection(exposure, policy):
    
    # common input test
    metadata   = exposure.getMetadata()

    crPolicy    = policy.getPolicy('crRejectionPolicy')
    # gain is LSST norm
    gainKeyword = 'gain' 
    gain        = metadata.get(gainKeyword)
    # needed for CR
    crPolicy.set('e_per_dn', gain)

    mi = exposure.getMaskedImage()

    # NOTE - this background issue needs to be resolved
    bg = 0.
    
    defaultFwhm = policy.get('defaultFwhm')
    psf         = algorithms.createPSF('DoubleGaussian', 0, 0, defaultFwhm/(2*math.sqrt(2*math.log(2))))
    crs         = algorithms.findCosmicRays(mi, psf, bg, crPolicy, False)    
    
    

def saturationCorrection(exposure, saturation, fwhm, growSaturated = False,
                         interpolate = True,
                         maskName    = 'SAT'):

    mi         = exposure.getMaskedImage()
    mask       = mi.getMask()
    bitmask    = mask.getPlaneBitMask(maskName)

    # find saturated regions
    thresh     = afwDetection.Threshold(saturation)
    ds         = afwDetection.FootprintSetF(mi, thresh)
    fpList     = ds.getFootprints()
    # we will turn them into defects for interpolating
    defectList = algorithms.DefectListT()
    
    # grow them
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
        mask.addMaskPlane('INTRP')
        psf = algorithms.createPSF('DoubleGaussian', 0, 0, fwhm/(2*math.sqrt(2*math.log(2))))
        algorithms.interpolateOverDefects(mi, psf, defectList)
    

def biasCorrection(exposure, bias):

    mi  = exposure.getMaskedImage()
    bmi = bias.getMaskedImage()
    mi -= bmi


def darkCorrection(exposure, dark, expscaling, darkscaling):
    
    scale             = expscaling / darkscaling
    mi  = exposure.getMaskedImage()
    mi.scaledMinus(scale, dark.getMaskedImage())

    
                   
def flatCorrection(exposure, flat, scalingtype, scaling = 1.0):

    flatscaling = 1.0
    # Figure out scaling from the data
    if scalingtype == 'MEAN':
        flatscaling = afwMath.makeStatistics(flat.getMaskedImage().getImage(), afwMath.MEAN).getValue(afwMath.MEAN)
    elif scalingtype == 'MEDIAN':
        flatscaling = afwMath.makeStatistics(flat.getMaskedImage().getImage(), afwMath.MEDIAN).getValue(afwMath.MEDIAN)
    elif scalingtype == 'USER':
        flatscaling = scaling
    else:
        raise pexExcept.LsstException, '%s : %s not implemented' % ("flatCorrection", scalingtype)            

    mi   = exposure.getMaskedImage()
    fmi  = flat.getMaskedImage()
    mi.scaledDivides(1./flatscaling, fmi)
    


def illuminationCorrection(exposure, illum, illumscaling):

    # common input test

    mi   = exposure.getMaskedImage()
    mi.scaledDivides(1./illumscaling, illum.getMaskedImage())
    


def trimNew(exposure, ampBBox, trimsec=None, trimsecKeyword='trimsec'):
    """
    This returns a new Exposure that is a subsection of the input exposure.
    
    NOTE : do we need to deal with the WCS in any way, shape, or form?
    """
    methodName = 'trimNew'
    
    # common input test

    metadata   = exposure.getMetadata()

    if metadata.exists(trimsecKeyword) and trimsec==None:
        trimsec = metadata.getString(trimsecKeyword)


    if trimsec == None:
        raise pexExcept.LsstException, '%s : cannot find trimsec' % (methodName)        

    trimsecBBox = isrLib.BBoxFromDatasec(trimsec)

    if not (trimsecBBox.getDimensions() == ampBBox.getDimensions()):
        raise pexException.LsstException, '%s : amp bounding box not same as\
        trim section'%(methodName)

    trimmedExposure = afwImage.ExposureF(exposure, trimsecBBox)
    trimmedExposure.getMaskedImage().setXY0(ampBBox.getLLC())

    # remove trimsec from metadata
    trimmedExposure.getMetadata().remove(trimsecKeyword)
    # n.b. what other changes are needed here?
    # e.g. wcs info, overscan, etc
    

    return trimmedExposure



def overscanCorrection(exposure, overscanBBox, fittype, overscanKeyword =
        'overscan', polyorder = 1):
    """
    """

    # common input test
    mi = exposure.getMaskedImage()

    # if "True", do a deep copy
    overscanData    = afwImage.ImageF(exposure.getMaskedImage().getImage(), overscanBBox, False)

    # what type of overscan modeling?
    if fittype == 'MEAN':
        offset = afwMath.makeStatistics(overscanData, afwMath.MEAN).getValue(afwMath.MEAN)
        mi    -= offset
    elif fittype == 'MEDIAN':
        offset = afwMath.makeStatistics(overscanData, afwMath.MEDIAN).getValue(afwMath.MEDIAN)
        mi    -= offset
    elif fittype == 'POLY':
        raise pexExcept.LsstException, '%s : %s not implemented' % ("overscanCorrection", fittype)
    else:
        raise pexExcept.LsstException, '%s : %s an invalid overscan type' % ("overscanCorrection", fittype)

    # remove overscan from metadata
    exposure.getMetadata().remove(overscanKeyword)
    return 


def fringeCorrection(exposure, fringe):

    raise pexExcept.LsstException, '%s not implemented' % ("ipIsr.fringCorrection")


def pupilCorrection(exposure, pupil):

    raise pexExcept.LsstException, '%s not implemented' % (stageName)