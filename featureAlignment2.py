#!/usr/bin/python

import sys, os, utils, numpy as np, pandas as pd
import rpy2.robjects as ro
from rpy2.robjects.vectors import IntVector, FloatVector

def calibrateFeatures(ref, comp, parameters):
    # ref and comp are "ndarray"s with the following column name
    # 'index' = nominal feature index
    # 'mz' = m/z value of a feature
    # 'z' = charge of the feature (0 for undetermined)
    # 'MS1ScanNumber' = representative MS1 scan number of the feature
    # 'minMS1ScanNumber' = minimum MS1 scan number of the feature (spanned)
    # 'maxMS1ScanNumber' = maximum MS1 scan number of the feature (spanned)
    # 'RT' = representative RT (should correspond to MS1ScanNumber)
    # 'minRT' = minimum RT
    # 'maxRT' = maximum RT
    # "intensity" = intensity of the feature
    # 'SN' = signal-to-noise ratio
    # 'PercentageTF' = percentage of true feature (= feature width over RT/scan)

    initMzTol = int(params["tol_initial"])
    sdWidth = int(params["sd_width"])
    ref = ref.sort_values("intensity", ascending = False, ignore_index = True)
    comp = comp.sort_values("intensity", ascending = False, ignore_index = True)

    # Calibration of RT and m/z globally
    print ("  Global calibration of features is being performed")
    rtShifts, mzShifts = globalCalibration(ref, comp, initMzTol)
    print ("    Based on the matched features within %d ppm" % initMzTol)
    print ("    The global RT-shift is %.4f second" % np.median(rtShifts))
    print ("    The global m/z-shift is %.4f ppm" % np.median(mzShifts))
    print ("    RT and m/z of the compared features are calibrated according to the above information")
    comp["RT"] = comp["RT"] - np.median(rtShifts)
    comp["mz"] = comp["mz"] / (1 + np.median(mzShifts) / 1e6)

    # Calibration of RT and m/z locally using LOESS (stepwise)
    rLoess = loess()
    rPredict = ro.r("predict")
    print ("  Local calibration of features is being performed (through LOESS modeling)")
    print ("    RT- and m/z-tolerances will be dynamically estimated over RT- and m/z-range as follows,")
    print ("    RT- and m/z-tolerance = %d x dynamically estimated SD of RT- and m/z-shifts" % sdWidth)
    print ("    LOESS modeling may take some time. Please be patient ...")
    rtSd = np.maximum(1e-3, np.std(rtShifts, ddof = 1))
    mzSd = np.maximum(1e-3, np.std(mzShifts, ddof = 1))
    ref, comp, rtSd, mzSd = localCalibration(ref, comp, rtSd, mzSd, rLoess, rPredict, params, "RT")
    print ("    The 1st round of RT-calibration is done")
    print ("      min SD of RT-shifts = %.4f second" % np.amin(rtSd))
    print ("      max SD of RT-shifts = %.4f second" % np.amax(rtSd))
    ref, comp, rtSd, mzSd = localCalibration(ref, comp, rtSd, mzSd, rLoess, rPredict, params, "RT")
    print ("    The 2nd round of RT-calibration is done")
    print ("      min SD of RT-shifts = %.4f second" % np.amin(rtSd))
    print ("      max SD of RT-shifts = %.4f second" % np.amax(rtSd))
    ref, comp, rtSd, mzSd = localCalibration(ref, comp, rtSd, mzSd, rLoess, rPredict, params, "mz")
    print ("    The 1st round of m/z-calibration is done")
    print ("      min SD of m/z-shifts = %.4f ppm" % np.amin(mzSd))
    print ("      max SD of m/z-shifts = %.4f ppm" % np.amax(mzSd))
    ref, comp, rtSd, mzSd = localCalibration(ref, comp, rtSd, mzSd, rLoess, rPredict, params, "mz")
    print ("    The 2nd round of m/z-calibration is done")
    print ("      min SD of m/z-shifts = %.4f ppm" % np.amin(mzSd))
    print ("      max SD of m/z-shifts = %.4f ppm" % np.amax(mzSd))
    print ()
    return comp, rtSd, mzSd # "comp" is the set of calibrated features


def globalCalibration(ref, comp, mzTol = 20):
    nPeaks = round(0.05 * ref.shape[0])    # Number of peaks to be considered for global calibration
    rtShifts = []   # Array for RT-shifts between reference and compared runs
    mzShifts = []   # Array for mz-shifts (ppm)
    i, j = 0, 1
    while j <= nPeaks:
        z = ref["z"][i] # From the 1st feature of reference run (i.e. strongest feature)
        mz = ref["mz"][i]
        lL = mz - mz * mzTol / 1e6
        uL = mz + mz * mzTol / 1e6
        rt = ref["RT"][i]
        if z == 0:
            # For a reference feature with undetermined charge, consider all possible charges in compared feature
            # rowInd = np.where((comp["mz"] >= lL) & (comp["mz"] < uL))[0]
            rowInd = comp[(comp["mz"] >= lL) & (comp["mz"] < uL)].index
        else:
            # rowInd = np.where((comp["mz"] >= lL) & (comp["mz"] < uL) & (comp['z'] == z))[0]
            rowInd = comp[(comp["mz"] >= lL) & (comp["mz"] < uL) & (comp["z"] == z)].index
        if len(rowInd) > 0:
            rowInd = rowInd[0]
            rtShifts.append(comp["RT"][rowInd] - rt)
            mzShifts.append((comp["mz"][rowInd] - mz) / mz * 1e6)
            comp = comp.drop(rowInd)
            j += 1

        i += 1

    # For more robust calculation, top and bottom 10% values are trimmed
    rtShifts = np.array(rtShifts)
    rtShifts = rtShifts[(rtShifts >= np.percentile(rtShifts, 10)) &
                     (rtShifts <= np.percentile(rtShifts, 90))]
    mzShifts = np.array(mzShifts)
    mzShifts = mzShifts[(mzShifts >= np.percentile(mzShifts, 10)) &
                     (mzShifts <= np.percentile(mzShifts, 90))]
    return rtShifts, mzShifts


def loess():
    rstring = """
    loess.as = function(x, y, degree = 1, criterion="aicc", family="gaussian", user.span=NULL, plot=FALSE, ...) {
        
        criterion <- match.arg(criterion)
        family <- match.arg(family)
        x <- as.matrix(x)
        
        if ((ncol(x) != 1) & (ncol(x) != 2)) stop("The predictor 'x' should be one or two dimensional!!")
        if (!is.numeric(x)) stop("argument 'x' must be numeric!")
        if (!is.numeric(y)) stop("argument 'y' must be numeric!")
        if (any(is.na(x))) stop("'x' contains missing values!")
        if (any(is.na(y))) stop("'y' contains missing values!")
        if (!is.null(user.span) && (length(user.span) != 1 || !is.numeric(user.span))) 
            stop("argument 'user.span' must be a numerical number!")
        if(nrow(x) != length(y)) stop("'x' and 'y' have different lengths!")
        if(length(y) < 3) stop("not enough observations!")
        
        data.bind <- data.frame(x=x, y=y)
        if (ncol(x) == 1) {
            names(data.bind) <- c("x", "y")
        } else { names(data.bind) <- c("x1", "x2", "y") }
        
        opt.span <- function(model, criterion=c("aicc", "gcv"), span.range=c(.05, .95)){	
            as.crit <- function (x) {
                span <- x$pars$span
                traceL <- x$trace.hat
                sigma2 <- sum(x$residuals^2 ) / (x$n-1)
                aicc <- log(sigma2) + 1 + 2* (2*(traceL+1)) / (x$n-traceL-2)
                gcv <- x$n*sigma2 / (x$n-traceL)^2
                result <- list(span=span, aicc=aicc, gcv=gcv)
                return(result)
            }
            criterion <- match.arg(criterion)
            fn <- function(span) {
                mod <- update(model, span=span)
                as.crit(mod)[[criterion]]
            }
            result <- optimize(fn, span.range)
            return(list(span=result$minimum, criterion=result$objective))
        }
        
        control = loess.control(surface = "direct")
        if (ncol(x)==1) {
            if (is.null(user.span)) {
                fit0 <- loess(y ~ x, degree=degree, family=family, data=data.bind, control=control, ...)
                span1 <- opt.span(fit0, criterion=criterion)$span
            } else {
                span1 <- user.span
            }		
            fit <- loess(y ~ x, degree=degree, span=span1, family=family, data=data.bind, control=control, ...)
        } else {
            if (is.null(user.span)) {
                fit0 <- loess(y ~ x1 + x2, degree=degree,family=family, data.bind, control=control, ...)
                span1 <- opt.span(fit0, criterion=criterion)$span
            } else {
                span1 <- user.span
            }		
            fit <- loess(y ~ x1 + x2, degree=degree, span=span1, family=family, data=data.bind, control=control...)
        }
        if (plot){
            if (ncol(x)==1) {
                m <- 100
                x.new <- seq(min(x), max(x), length.out=m)
                fit.new <- predict(fit, data.frame(x = x.new))
                plot(x, y, col="lightgrey", xlab="x", ylab="m(x)", ...)
                lines(x.new,fit.new, lwd=1.5, ...)
            } else {
                m <- 50
                x1 <- seq(min(data.bind$x1), max(data.bind$x1), len=m) 
                x2 <- seq(min(data.bind$x2), max(data.bind$x2), len=m) 
                x.new <- expand.grid(x1=x1, x2=x2) 
                fit.new <- matrix(predict(fit, x.new), m, m) 
                persp(x1, x2, fit.new, theta=40, phi=30, ticktype="detailed", xlab="x1", ylab="x2", zlab="y", col="lightblue", expand=0.6)
            }		
        }
        return(fit)
    }
    """
    return ro.r(rstring)


def findMatchedSubset(ref, comp, rtSd, mzSd, params):
    ref = ref.sort_values("intensity", ascending = False, ignore_index = True)
    comp = comp.sort_values("intensity", ascending = False, ignore_index = True)

    n = ref.shape[0]
    if not isinstance(rtSd, (list, np.ndarray)):
        rtSd = np.repeat(rtSd, n)
    if not isinstance(mzSd, (list, np.ndarray)):
        mzSd = np.repeat(mzSd, n)
    rtSd[rtSd == 0] = min(rtSd[rtSd > 0])
    mzSd[mzSd == 0] = min(mzSd[mzSd > 0])
    sdWidth = float(params["sd_width"])
    rtTol = rtSd * sdWidth
    mzTol = mzSd * sdWidth

    # Look for a small dataframe containing matched features between "ref" and "comp"
    # subDf = pd.DataFrame(columns = ["refMz", "refRt", "refIntensity", "compMz", "compRt", "compIntensity"])
    dict = {}
    j = 0
    for i in range(n):   # For each feature in "ref", look for a matching one in "comp"
        z = ref["z"][i]
        mz = ref["mz"][i]
        rt = ref["RT"][i]
        intensity = ref["intensity"][i]
        rtDev = comp["RT"] - rt
        mzDev = (comp["mz"] - mz) / mz * 1e6    # Unit of PPM
        if z == 0:  # Undetermined charge
            # For the feature with undetermined charge,
            # look for a matching one without considering charge state
            rowInd = comp[(abs(rtDev) <= rtTol[i]) & (abs(mzDev) <= mzTol[i])].index
        else:
            # For the feature with a charge state,
            # look for a matching one with considering charge state
            rowInd = comp[(abs(rtDev) <= rtTol[i]) & (abs(mzDev) <= mzTol[i]) & (comp['z'] == z)].index
        if len(rowInd) > 0:
            # When multiple features in "comp" are matched to a feature in "ref",
            # choose the one with the highest intensity
            # Since "comp" is sorted by descending order of intensity,
            # the first one has the highest intensity
            rowInd = rowInd[0]
            dict[j] = {"refMz": mz, "refRt": rt, "refIntensity": intensity,
                       "compMz": comp["mz"][rowInd], "compRt": comp["RT"][rowInd], "compIntensity": comp["intensity"][rowInd]}
            j += 1

    subDf = pd.DataFrame.from_dict(dict, "index")
    return subDf


def localCalibration(ref, comp, rtSd, mzSd, rLoess, rPredict, params, type):
    subDf = findMatchedSubset(ref, comp, rtSd, mzSd, params)

    # Perform LOESS regression to calibrated RT and m/z
    refRt = subDf["refRt"]
    compRt = subDf["compRt"]
    refMz = subDf["refMz"]
    compMz = subDf["compMz"]

    if type == "RT":    # RT-calibration
        if (refRt == compRt).all():
            rtShifts = 1e-6 * np.random.normal(len(refRt))
        else:
            rtShifts = compRt - refRt
        mod = rLoess(FloatVector(compRt), FloatVector(rtShifts))
        compRt = compRt - np.array(mod.rx2("fitted"))   # Calibrated RT based on the model

        # Calculate a new (dynamic) RT-tolerance
        if (refRt == compRt).all():
            rtShifts = 1e-6 * np.random.normal(len(refRt))
        else:
            rtShifts = compRt - refRt   # Calibrated RT-shifts
        ind = np.where((rtShifts >= np.percentile(rtShifts, 10)) &
                       (rtShifts <= np.percentile(rtShifts, 90)))[0]
        modRtSd = rLoess(FloatVector(compRt[ind]), FloatVector(rtShifts[ind] ** 2))
        rtSd = np.sqrt(np.maximum(0, rPredict(modRtSd, FloatVector(ref["RT"]))))

        # Calculate a new (dynamic) m/z-tolerance
        if (refMz == compMz).all():
            mzShifts = 1e-6 * np.random.normal(len(refMz))
        else:
            mzShifts = (compMz - refMz) / refMz * 1e6
        # Sometimes, the variation of mzShifts cannot be captured when trimming is applied
        # so, the trimming is not used for mzShifts
        modMzSd = rLoess(FloatVector(compMz), FloatVector(mzShifts ** 2), 1, "aicc", "gaussian")
        mzSd = np.sqrt(np.maximum(0, rPredict(modMzSd, FloatVector(ref["mz"]))))

        # Calibration of the entire comp["RT"]
        comp["RT"] = comp["RT"] - rPredict(mod, FloatVector(comp["RT"]))
    elif type == "mz":  # m/z-calibration
        if (refMz == compMz).all():
            mzShifts = 1e-6 * np.random.normal(len(refMz))
        else:
            mzShifts = (compMz - refMz) / refMz * 1e6
        mod = rLoess(FloatVector(compMz), FloatVector(mzShifts), 1, "aicc", "gaussian")
        compMz = compMz * (1 + np.array(mod.rx2("fitted")) / 1e6)   # Calibrated m/z basd on the model

        # Calculate a new (dynamic) m/z-tolerance
        if (refMz == compMz).all():
            mzShifts = 1e-6 * np.random.normal(len(refMz))
        else:
            mzShifts = (compMz - refMz) / refMz * 1e6   # Calibrated m/z-shifts
        modMzSd = rLoess(FloatVector(compMz), FloatVector(mzShifts ** 2), 1, "aicc", "gaussian")
        mzSd = np.sqrt(np.maximum(0, rPredict(modMzSd, FloatVector(ref["mz"]))))

        # Calculate a new (dynamic) RT-tolerance
        if (refRt == compRt).all():
            rtShifts = 1e-6 * np.random.normal(len(refRt))
        else:
            rtShifts = compRt - refRt
        ind = np.where((rtShifts >= np.percentile(rtShifts, 10)) &
                       (rtShifts <= np.percentile(rtShifts, 90)))[0]
        modRtSd = rLoess(FloatVector(compRt[ind]), FloatVector(rtShifts[ind] ** 2), 1, "aicc", "gaussian")
        rtSd = np.sqrt(np.maximum(0, rPredict(modRtSd, FloatVector(ref["RT"]))))

        # Calibration of the entire comp["mz"]
        comp["mz"] = comp["mz"] / (1 + np.array(rPredict(mod, FloatVector(comp["mz"]))) / 1e6)

    return ref, comp, rtSd, mzSd


def matchFeatures(ref, comp, rtSd, mzSd, params, step = "calibration"):
    ref = ref.sort_values("intensity", ascending=False, ignore_index=True)
    comp = comp.sort_values("intensity", ascending=False, ignore_index=True)

    n = ref.shape[0]
    if not isinstance(rtSd, (list, np.ndarray)):
        rtSd = np.repeat(rtSd, n)
    if not isinstance(mzSd, (list, np.ndarray)):
        mzSd = np.repeat(mzSd, n)
    rtSd[rtSd == 0] = min(rtSd[rtSd > 0])
    mzSd[mzSd == 0] = min(mzSd[mzSd > 0])
    sdWidth = float(params["sd_width"])
    rtTol = rtSd * sdWidth
    mzTol = mzSd * sdWidth

    # Look for matching features between "ref" and "comp"
    d = {}
    j = 0
    for i in range(n):   # For each feature in "ref", look for a matching one in "comp"
        z = ref["z"][i]
        mz = ref["mz"][i]
        rt = ref["RT"][i]
        rtDev = comp["RT"] - rt
        mzDev = (comp["mz"] - mz) / mz * 1e6    # Unit of PPM
        rowInd = comp[(abs(rtDev) <= rtTol[i]) & (abs(mzDev) <= mzTol[i])].index

        # When there is/are matched feature(s) in "comp" run
        if len(rowInd) > 0:
            if z == 0:
                rowInd = rowInd[0]
                dr = ref.iloc[i].to_dict()
                dr = dict((str("ref_") + k, v) for (k, v) in dr.items())
                dc = comp.loc[rowInd].to_dict()
                dc = dict((str("comp_") + k, v) for (k, v) in dc.items())
                d[j] = {**dr, **dc}
                j += 1
                comp = comp.drop(rowInd)
            else:
                if comp["z"][rowInd[0]] == 0:
                    rowInd = rowInd[0]
                    dr = ref.iloc[i].to_dict()
                    dr = dict((str("ref_") + k, v) for (k, v) in dr.items())
                    dc = comp.loc[rowInd].to_dict()
                    dc = dict((str("comp_") + k, v) for (k, v) in dc.items())
                    d[j] = {**dr, **dc}
                    j += 1
                    comp = comp.drop(rowInd)
                else:
                    # For the feature with a charge state,
                    # look for a matching one with considering charge state
                    rowInd = comp[(abs(rtDev) <= rtTol[i]) & (abs(mzDev) <= mzTol[i]) & (comp["z"] == z)].index
                    if len(rowInd) > 0:
                        rowInd = rowInd[0]
                        dr = ref.iloc[i].to_dict()
                        dr = dict((str("ref_") + k, v) for (k, v) in dr.items())
                        dc = comp.loc[rowInd].to_dict()
                        dc = dict((str("comp_") + k, v) for (k, v) in dc.items())
                        d[j] = {**dr, **dc}
                        j += 1
                        comp = comp.drop(rowInd)
        else:
            continue



    # Optional/advanced function for rescuing some "unaligned" features by loosening tolerances
    '''
    if step != "calibration" and params["rescue"] == "1":
        rtTolUnit = params["rt_tolerance_unit"].split(",")
        rtTol = params["rt_tolerance_value"].split(",")
        mzTolUnit = params["mz_tolerance_unit"].split(",")
        mzTol = params["mz_tolerance_value"].split(",")
        for i in range(len(rtTol)):
            refInd, compInd = rescueFeatures(ref, comp, refInd, compInd, rtSd, mzSd,
                                             rtTolUnit[i], rtTol[i], mzTolUnit[i], mzTol[i])
    '''

    df = pd.DataFrame.from_dict(d, "index")
    return df


def rescueFeatures(ref, comp, refInd, compInd, rtSd, mzSd, rtTolUnit, rtTol, mzTolUnit, mzTol):
    nRescue = 0
    nUnaligned = comp.shape[0] - len(compInd)
    print ("    There are %d unaligned features" % nUnaligned)

    # 1. Reduce variables to ones of unaligned features
    n = ref.shape[0]
    uRefInd = np.setdiff1d(range(n), refInd)
    rtSd = rtSd[uRefInd]
    mzSd = mzSd[uRefInd]
    uRef = ref[uRefInd]
    nc = comp.shape[0]
    uCompInd = np.setdiff1d(range(nc), compInd)
    uComp = comp[uCompInd]

    # 2. Criteria for rescuing unaligned features
    #    - Absolute intensity level: hard-coded (grand median intensity of aligned features)
    #    - Intensity-ratio between ref and comp runs: hard-coded (within 95% of the ratios of aligned features)
    #    - RT- and m/z-shifts should be within specified tolerance (e.g. 10SD or 10ppm)
    intLevel = np.ma.median(comp["intensity"][compInd])
    ratioPct = 95
    ratioAligned = np.log2(ref["intensity"][refInd]) - np.log2(comp["intensity"][compInd])
    lRatio = np.percentile(ratioAligned, (100 - ratioPct) / 2)
    uRatio = np.percentile(ratioAligned, 100 - (100 - ratioPct) / 2)
    print ("    - Intensity higher than %d (median intensity of aligned features)" % intLevel)
    print ("    - Ratio between reference and compared runs within %d %%" % ratioPct)
    if rtTolUnit == "1":
        print ("    - RT-shifts within %s x SD of estimated RT-shifts from aligned features" % rtTol)
        rtTol = float(rtTol) * rtSd
    elif rtTolUnit == "2":
        print ("    - RT-shifts less than %s seconds" % rtTol)
        rtTol = np.repeat(float(rtTol), len(uRefInd))
    else:
        print ("    WARNING: check your parameters for RT-tolerance unit. It should be either 1 or 2")
        print ("    Due to incorrect parameter settings, the rescue step is skipped")
        return refInd, compInd

    if mzTolUnit == "1":
        print ("    - m/z-shifts within %s x SD of estimated m/z-shifts from aligned features" % mzTol)
        mzTol = float(mzTol) * mzSd
    elif mzTolUnit == "2":
        print ("    - m/z-shifts less than %s seconds" % mzTol)
        mzTol = np.repeat(float(mzTol), len(uRefInd))
    else:
        print ("    WARNING: check your parameters for RT-tolerance unit. It should be either 1 or 2")
        print ("    Due to incorrect parameter settings, the rescue step is skipped")
        return refInd, compInd

    # 3. Apply the criteria to unaligned features
    for i in range(len(uRefInd)):
        intRatios = np.log2(uComp["intensity"]) - np.log2(uRef["intensity"][i])
        rtShifts = uComp["RT"] - uRef["RT"][i]
        mzShifts = (uComp["mz"] - uRef["mz"][i]) / uRef["mz"][i] * 1e6
        ind = np.where((uComp["intensity"] > intLevel) & (intRatios >= lRatio) & (intRatios <= uRatio) &
                       (abs(mzShifts) <= mzTol[i]) & (abs(rtShifts) <= rtTol[i]))[0]
        if len(ind) > 0:
            charge = uRef["z"][i]
            if charge == 0:
                ind = ind[0]
                refInd.append(uRefInd[i])
                compInd.append(uCompInd[ind])
                nRescue += 1
            else:
                if uComp["z"][ind[0]] == 0:
                    ind = ind[0]
                    refInd.append(uRefInd[i])
                    compInd.append(uCompInd[ind])
                    nRescue += 1
                else:
                    ind2 = np.where(uComp["z"][ind] == charge)[0]
                    if len(ind2) > 0:
                        ind2 = ind2[0]
                        refInd.append(uRefInd[i])
                        compInd.append(uCompInd[ind2])
                        nRescue += 1

    print ("    Through the rescue procedure %d features are additionally aligned" % nRescue)
    return (refInd, compInd)


def alignFeatures(fArray, fNames, params):
    n = len(fNames)
    indArray = - np.ones((fArray[refNo].shape[0], n), dtype = int)
    indArray[:, refNo] = range(fArray[refNo].shape[0])
    for i in range(n):
        if i != refNo:
            refName = os.path.basename(featureFiles[refNo])
            compName = os.path.basename(featureFiles[i])
            print ("  " + refName + ": %d features (reference run)" % fArray[refNo].shape[0])
            print ("  " + compName + ": %d features (compared run)" % fArray[i].shape[0])
            refInd, compInd = matchFeatures(fArray[refNo], fArray[i], rtSdArray[i], mzSdArray[i], params, "align")

            # Alignment indication
            # indArray is [# reference features x # feature files] matrix
            # If reference is the 3rd run (i.e. feature file),
            # indArray[i, j] indicates the feature index of j-th run matched to i-th reference feature
            # indArray[i, j] = -1 means that there's no feature matched to i-th reference feature in j-th run
            rowInd = np.nonzero(np.in1d(indArray[:, refNo], refInd))[0]
            indArray[rowInd, i] = compInd
            print ("    %d features are aligned between runs" % len(rowInd))
    print ()

    # Indexes of fully- and partially-aligned features
    fullInd = range(indArray.shape[0])
    partialInd = []
    colNames = list(fArray[0].dtype.names)
    header = []
    for i in range(n):
        header.extend([fNames[i] + "_" + c for c in colNames])
        if i != refNo:
            fullInd = np.intersect1d(fullInd, np.where(indArray[:, i] >= 0)[0])
            partialInd = np.union1d(partialInd, np.where(indArray[:, i] >= 0)[0])
    partialInd = np.setdiff1d(partialInd, fullInd)
    partialInd = partialInd.astype(int)

    # Fully-aligned features
    nCols = len(fArray[0][0])   # Number of columns in each feature array
    full = np.zeros((len(fullInd), n * nCols))
    for i in range(len(fullInd)):
        for j in range(n):
            colInd = indArray[fullInd[i], j]
            if j == 0:
                line = list(fArray[j][colInd])
            else:
                line.extend(list(fArray[j][colInd]))
        full[i, :] = line

    # Partially-aligned features
    partial = - np.ones((len(partialInd), n * nCols))
    for i in range(len(partialInd)):
        for j in range(n):
            colInd = indArray[partialInd[i], j]
            if j == 0:
                if colInd == -1:
                    line = [-1] * nCols
                else:
                    line = list(fArray[j][colInd])
            else:
                if colInd == -1:
                    line.extend(([-1] * nCols))
                else:
                    line.extend(list(fArray[j][colInd]))
        partial[i, :] = line
    partial[partial == -1] = np.nan

    # Un-aligned features
    unaligned = []
    for i in range(n):
        if i == refNo:
            rowInd = np.setdiff1d(range(fArray[refNo].shape[0]), np.union1d(fullInd, partialInd))
            unaligned.append(fArray[refNo][rowInd, ])
        else:
            rowInd = np.setdiff1d(range(fArray[i].shape[0]), indArray[:, i])
            unaligned.append(fArray[i][rowInd, ])

    # Alignment summary
    print ("  Alignment/match summary")
    print ("  =======================")
    print ("    After alignment/feature matching, fully-, partially- and un-aligned features are as follows")
    print ("    Filename\t\tfully-aligned\tpartially-aligned\tun-aligned")
    nFull = len(fullInd)
    for i in range(n):
        if i == refNo:
            nPartial = len(partialInd)
        else:
            nPartial = np.sum(indArray[:, i] >= 0) - len(fullInd)
        nUn = unaligned[i].shape[0]
        print ("    %s\t\t%d\t%d\t%d" % (featureNames[i], nFull, nPartial, nUn))

    # Depending on the parameter, "pct_full_alignment", some partially-aligned features can be included in fully-aligned ones
    pctFullAlignment = float(params["pct_full_alignment"])
    if pctFullAlignment < 100:
        colInd = [i for i, col in enumerate(header) if col.endswith('index')]
        nRuns = np.sum(~np.isnan(partial[:, colInd]), axis = 1)  # For each partially-aligned feature, the number of aligned runs (i.e. feature files)
        rowInd = np.where(nRuns >= np.ceil(pctFullAlignment / 100 * nFiles))[0]

        # Add some partially-aligned features to fully-aligned features
        full = np.vstack((full, partial[rowInd, :]))
        partial = np.delete(partial, rowInd, axis = 0)
        print ("    According to the parameter setting, %d partially-aligned features are regarded as fully-aligned" % len(rowInd))
    else:
        print ("    According to the parameter setting, no feature is added to the set of fully-aligned ones")

    # Change ndarrays to masked/structured arrays
    full = np.core.records.fromarrays(full.transpose(), names = tuple(header))
    partial = np.core.records.fromarrays(partial.transpose(), names = tuple(header))
    # unaligned is already a masked/structured array

    return full, partial, unaligned


###########################################
################ Main part ################
###########################################

paramFile = r"U:\Research\Projects\7Metabolomics\JUMPm\IROAsamples\jumpm_negative.params"
featureFiles = [r"U:\Research\Projects\7Metabolomics\JUMPm\IROAsamples\IROA_IS_NEG_1.1.dev.feature",
                r"U:\Research\Projects\7Metabolomics\JUMPm\IROAsamples\IROA_IS_NEG_2.1.dev.feature",
                r"U:\Research\Projects\7Metabolomics\JUMPm\IROAsamples\IROA_IS_NEG_3.1.dev.feature"]

nFiles = len(featureFiles)

################################
# Load parameters and features #
################################
params = utils.getParams(paramFile)
# Features from .feature files are stored in fArray. For example,
# featureFiles = [file1, file2, file3]
# fArray[0] = features from file1 (which has column names like 'index', 'mz', etc.)
# fArray[1] = features from file2
# ...
# The array of m/z values from the first feature file can be accessed by fArray[0]['mz']
# The header of .feature file is used as column names of the array
# Note that "/" (slash) is ignored when the file is loaded through "genfromtxt"
fArray = []
for i in range(nFiles):
    df = pd.read_table(featureFiles[i], sep = "\t")


    # Change the column names
    df.rename(columns = {"m/z": "mz", "MS1ScanNumber": "MS1", "minMS1ScanNumber": "minMS1",
                         "maxMS1ScanNumber": "maxMS1", "S/N": "SNratio"},
              inplace = True)



    fArray.append(df)
    # data = np.genfromtxt(featureFiles[i], delimiter = "\t", dtype = None, names = True)
    # fArray.append(data)

if nFiles > 1: # Multiple feature files -> alignment is required
    print ("  Feature calibration")
    print ("  ===================")

    ###################################
    # Selection of a reference sample #
    ###################################
    if params["reference_feature"] == "0":
        # A sample with the largest median of top 100 intensities is set to a reference run
        refNo = 0
        refIntensity = 0
        for i in range(nFiles):
            tmpIntensity = np.median(sorted(fArray[i]["intensity"], reverse = True)[0: 100])
            if tmpIntensity >= refIntensity:
                refNo = i
                refIntensity = tmpIntensity
    else:
        try:
            refNo = featureFiles.index(params["reference_feature"])
        except:
            sys.exit("  'reference_feature' parameter should be correctly specified")
    print ("  %s is chosen as the reference run" % os.path.basename(featureFiles[refNo]))

    ############################################################
    # Calibration of features against those in a reference run #
    ############################################################
    rtSdArray, mzSdArray = [], []
    featureNames = []
    for i in range(nFiles):
        featureName = os.path.basename(featureFiles[i])
        featureNames.append(featureName)
        if i != refNo:
            print ("  " + featureName + " is being aligned against the reference run (it may take a while)")
            fArray[i], rtSd, mzSd = calibrateFeatures(fArray[refNo], fArray[i], params)
            rtSdArray.append(rtSd)
            mzSdArray.append(mzSd)
        else:
            rtSdArray.append("NA")
            mzSdArray.append("NA")

    print ("  Calibration summary")
    print ("  After calibration, RT- and m/z-shifts of each run (against the reference run) are centered to zero")
    print ("  Variations (i.e. standard deviation) of RT- and m/z-shifts are as follows,")
    print ("  Filename\t\t\t#features\tSD of RT-shifts [second]\tSD of m/z-shifts [ppm]")
    for i in range(nFiles):
        nFeatures = str(fArray[i].shape[0])
        if i != refNo:
            meanRtSd = "%.6f" % np.mean(rtSdArray[i])
            meanMzSd = "%.6f" % np.mean(mzSdArray[i])
        else:
            meanRtSd = "NA"
            meanMzSd = "NA"
        print ("  " + featureNames[i] + "\t\t\t" + nFeatures + "\t" + meanRtSd + "\t" + meanMzSd)
    print ()

    #################################################################
    # Identification of fully-aligned features for further analysis #
    #################################################################
    print ("  Feature alignment")
    print ("  =================")
    fullFeatures, partialFeatures, unalignedFeatures = alignFeatures(fArray, featureNames, params)

    # 1. fullFeatures and partialFeatures are ndarrays
    # For example, there are 4 feature files and each feature has 13 columns (i.e. index, RT, minRT, etc.)
    # then, fullFeature and partialFeature have 4 x 13 (= 42) columns
    # 2. unalignedFeatures is a masked ndarray
    # As for the above example, len(unalignedFeatures) = 4 and unalignedFeatures[i] = a masked array with 13 columns






    # To-do : quantify features





else:
    print ("  Since a single feature is used, the feature alignment is skipped")
    fullFeatures = np.copy(fArray[0])    # Masked array to 2D numpy array

###########################
# Write features to files #
###########################
# This output file is for user's information
# Subsequent processes will be performed using "fullFeature" object instead of loading the file itself


# ####################################
# # Features to MS2 spectra matching #
# ####################################
# # This routine looks for MS2 spectra responsible for each feature
# # These MS2 spectra will be merged into a synthetic MS2 spectrum and then used for metabolite identification
#
# # Parameters
# tolIsolation = float(params["isolation_windows"])
# tolPrecursor = float(params["tol_precursor"])
# tolIntraMS2 = float(params["tol_intra_ms2_consolidation"])
# tolInterMS2 = float(params["tol_inter_ms2_consolidation"])
#
# # Investigate every MS2 spectrum in each run
# for i in range(nFiles):


print ()





