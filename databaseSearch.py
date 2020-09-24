import sys, os, utils, pandas as pd, multiprocessing as mp, time


def generateFiles(feature, params):
    num = feature["feature_num"]
    paramFile = "metfrag_params_" + str(num) + ".txt"
    ms2File = "metfrag_data_" + str(num) + ".txt"
    outputName = "metfrag_result_" + str(num)
    outputFile = "metfrag_result_" + str(num) + ".csv"
    proton = 1.007276466812
    mass = feature["feature_z"] * (feature["feature_m/z"] - proton) # Neutral (monoisotopic) mass

    # Parameter file for MetFrag
    f = open(paramFile, "w")
    f.write("PeakListPath = {}\n".format(ms2File))

    # Even though this code is designed to use a database including PubChem, PubChem is not available in practice due
    # to the following reasons. 1) Many queries to PubChem through MetFrag are not stable -> problem of MetFrag (
    # according to authors) 2) An alternative of using a local CSV file is possible, but it takes too long because of
    # the size of file (> 30GB)
    if params["database"].lower() == "pubchem":
        f.write("MetFragDatabaseType = PubChem\n")
    else:
        if os.path.isfile(params["database"]):
            f.write("MetFragDatabaseType = LocalCSV\n")
            f.write("LocalDatabasePath = {}\n".format(params["database"]))
        else:
            sys.exit("Please check the path of a database file (.csv)")
    f.write("DatabaseSearchRelativeMassDeviation = {}\n".format(params["mass_tolerance_formula_search"]))
    f.write("FragmentPeakMatchRelativeMassDeviation = {}\n".format(params["mass_tolerance_ms2_peaks"]))
    f.write("NeutralPrecursorMass = {}\n".format(mass))
    f.write("PrecursorIonMode = 1\n")   # It may contain adduct information. Refer https://ipb-halle.github.io/MetFrag/projects/metfragcl/
    if params["mode"] == "1":
        f.write("IsPositiveIonMode = True\n")
    elif params["mode"] == "-1":
        f.write("IsPositiveIonMode = False\n")
    f.write("MetFragScoreTypes = FragmenterScore\n")
    f.write("MetFragScoreWeights = 1.0\n")
    f.write("MetFragCandidateWriter = CSV\n")
    f.write("SampleName = {}\n".format(outputName))
    f.write("ResultsPath = .\n")
    f.write("MaximumTreeDepth = 2\n")
    f.write("MetFragPreProcessingCandidateFilter = UnconnectedCompoundFilter\n")
    f.write("MetFragPostProcessingCandidateFilter = InChIKeyFilter\n")
    f.close()

    # MS2 data file for MetFrag
    ms2Dict = feature["MS2"]
    df = pd.DataFrame.from_dict(ms2Dict, orient = "columns")
    df = df.drop([0])
    df.to_csv(ms2File, sep = "\t", index = False, header = False)

    return paramFile, ms2File, outputFile


def runMetFrag(feature, params):
    if feature["MS2"] is not None:
        paramFile, ms2File, outputFile = generateFiles(feature, params)

        # MetFrag should be installed first and its path should be put to the following command
        cmd = "java -jar " + params["metfrag"] + " " + paramFile + "> /dev/null 2>&1" # "> /dev/null 2>&1" is for linux only
        os.system(cmd)
        time.sleep(0.1)
        df = pd.read_csv(outputFile)
        if params["database"].lower() == "pubchem":
            df = df.rename(columns = {"IUPACName": "CompoundName"})
        df["feature_index"] = feature["feature_num"]
        df["feature_m/z"] = feature["feature_m/z"]
        df["feature_RT"] = feature["feature_RT"]
        df["feature_intensity"] = feature["feature_intensity"]
        columns = ["feature_index", "feature_m/z", "feature_RT", "feature_intensity",
                   "Identifier", "MolecularFormula", "CompoundName", "SMILES", "InChIKey", "Score"]
        df = df[columns]
        os.remove(paramFile)
        os.remove(ms2File)
        os.remove(outputFile)
        return df
    else:
        return None


def searchDatabase(features, paramFile):
    try:
        params = utils.getParams(paramFile)
    except:
        sys.exit("Parameter file cannot be found or cannot be loaded")
    pool = mp.Pool(int(mp.cpu_count() / 2))
    res = pool.starmap_async(runMetFrag, [(row.to_dict(), params) for idx, row in features.iterrows()])
    nTot = res._number_left
    progress = utils.progressBar(nTot)
    while not res.ready():
        nFinished = nTot - res._number_left
        progress.increment(nFinished)
        time.sleep(0.5)
    res.wait()
    progress.increment(nTot)
    pool.close()
    res = pd.concat(res.get(), ignore_index = True)
    res["feature_RT"] = res["feature_RT"] / 60  # Change the unit to minute (for output)
    filePath = os.path.join(os.getcwd(), "align_" + params["output_name"])
    outputFile = os.path.join(filePath, "align_" + params["output_name"] + ".database_matches")
    res.to_csv(outputFile, sep = "\t", index = False)

    return res
