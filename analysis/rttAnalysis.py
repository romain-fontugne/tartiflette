import sys
import itertools
from datetime import datetime
from datetime import timedelta
from pytz import timezone
import calendar
import time
import os
import json
import glob
import numpy as np
from collections import defaultdict
from collections import deque
from scipy import stats
import pymongo
from multiprocessing import Process, JoinableQueue, Manager, Pool
import tools
import statsmodels.api as sm
import cPickle as pickle
import geoip

def readOneTraceroute(trace, measuredRtt, inferredRtt, metric=np.nanmedian):
    """Read a single traceroute instance and compute the corresponding 
    measured and inferred RTTs.
    """

    if trace is None or "error" in trace["result"][0] or "err" in trace["result"][0]["result"]:
        return measuredRtt, inferredRtt

    ipProbe = "probe_%s"  % trace["prb_id"]
    ip2 = None
    prevRttList = {}

    for hopNb, hop in enumerate(trace["result"]):
        # print "i=%s  and hop=%s" % (hopNb, hop)

        try:
            # TODO: clean that workaround results containing no IP, e.g.:
            # {u'result': [{u'x': u'*'}, {u'x': u'*'}, {u'x': u'*'}], u'hop': 6}, 

            if "result" in hop :

                # rttList = np.array([np.nan]*len(hop["result"])) 
                rttList = defaultdict(list) 
                for res in hop["result"]:
                    if not "from" in res  or tools.isPrivateIP(res["from"]) or not "rtt" in res or res["rtt"] <= 0.0:
                        continue

                    # if hopNb+1!=hop["hop"]:
                        # print trace
                    assert hopNb+1==hop["hop"] or hop["hop"]==255 
                    ip0 = res["from"]
                    rtt =  res["rtt"]
                    # rttList[resNb] = rtt
                    rttList[ip0].append(rtt)


                for ip2, rtts in rttList.iteritems():
                    rttAgg = np.median(rtts)

                    # measured path
                    if not measuredRtt is None:
                        if (ipProbe, ip2) in measuredRtt:
                            m = measuredRtt[(ipProbe, ip2)]
                            m["rtt"].append(rttAgg)
                            m["probe"].add(ipProbe)
                        else:
                            measuredRtt[(ipProbe, ip2)] = {"rtt": [rttAgg], 
                                                        "probe": set([ipProbe])}

                    # Inferred rtt
                    if not inferredRtt is None and len(prevRttList):
                        for ip1, prevRtt in prevRttList.iteritems():
                            if ip1 == ip2:
                                continue
                            prevRttAgg = np.median(prevRtt)
                            if (ip2,ip1) in inferredRtt:
                                i = inferredRtt[(ip2,ip1)]
                                i["rtt"].append(rttAgg-prevRttAgg)
                                i["probe"].add(ipProbe)
                            elif (ip1, ip2) in inferredRtt:
                                i = inferredRtt[(ip1,ip2)]
                                i["rtt"].append(rttAgg-prevRttAgg)
                                i["probe"].add(ipProbe)
                            else:
                                inferredRtt[(ip1,ip2)] = {"rtt": [rttAgg-prevRttAgg],
                                                        "probe": set([ipProbe])}

        finally:
            prevRttList = rttList
            # TODO we miss 2 inferred links if a router never replies

    return measuredRtt, inferredRtt



######## used by child processes
collection = None

def processInit():
    global collection
    client = pymongo.MongoClient("mongodb-iijlab",connect=True)
    db = client.atlas
    collection = db.traceroute_2015_12

def computeRtt( (start, end) ):
    """Read traceroutes from a cursor. Used for multi-processing.
    """

    tsS = time.time()
    nbRow = 0
    measuredRtt = None
    inferredRtt = defaultdict(dict)
    tsM = time.time()
    cursor = collection.find( { "timestamp": {"$gte": start, "$lt": end}} , 
            projection={"timestamp": 1, "result":1, "prb_id":1} , 
            cursor_type=pymongo.cursor.CursorType.EXHAUST,
            batch_size=int(10e6))
    tsM = time.time() - tsM
    for trace in cursor: 
        readOneTraceroute(trace, measuredRtt, inferredRtt)
        nbRow += 1
    timeSpent = time.time()-tsS
    # print("Worker %0.1f /sec., dict size: (%s, %s), mongo time: %s, total time: %s"
             # % (float(nbRow)/(timeSpent), len(measuredRtt),len(inferredRtt), tsM, timeSpent))

    return measuredRtt, inferredRtt, nbRow

######## used by child processes

def mergeRttResults(rttResults):

        measuredRtt = None
        inferredRtt = defaultdict(dict)
        nbRow = 0 
        for mRtt, iRtt, compRows in rttResults:
            if not mRtt is None:
                for k, v in mRtt.iteritems():
                    if k in measuredRtt:
                        m = measuredRtt[k]
                        m["rtt"].extend(v["rtt"])
                        m["probe"].update(v["probe"])
                    else:
                        measuredRtt[k] = v

            for k, v in iRtt.iteritems():
                if k in inferredRtt:
                    i = inferredRtt[k]
                    i["rtt"].extend(v["rtt"])
                    i["probe"].update(v["probe"])
                else:
                    inferredRtt[k] = v

            nbRow += compRows

        return measuredRtt, inferredRtt, nbRow


def outlierDetection(sampleDistributions, smoothMean, param, expId, ts, 
    collection=None):

    if sampleDistributions is None:
        return

    alarms = []
    otherParams={}
    metrics = param["metrics"]
    alpha = float(param["alpha"])
    minProbes= param["minProbes"]
    confInterval = param["confInterval"]

    for ipPair, data in sampleDistributions.iteritems():

        dist = data["rtt"]
        nbProbes = len(data["probe"])
        n = len(dist) 
        # Compute the distribution median
        if nbProbes < minProbes:
            continue
        med = np.median(dist)
        wilsonCi = sm.stats.proportion_confint(len(dist)/2, len(dist), confInterval, "wilson")
        wilsonCi = np.array(wilsonCi)*len(dist)
        dist.sort()
        currLow = dist[int(wilsonCi[0])]
        currHi = dist[int(wilsonCi[1])]

        if ipPair in smoothMean: 
            # detection
            ref = smoothMean[ipPair]
    
            if ref["high"] < currLow or ref["low"] > currHi:
                if med < ref["mean"]:
                    diff = currHi - ref["low"]
                    diffMed = med - ref["mean"]
                    deviation = diff / (ref["low"]-ref["mean"])
                else:
                    diff = currLow - ref["high"]
                    diffMed = med - ref["mean"]
                    deviation = diff / (ref["high"]-ref["mean"])

                alarm = {"timeBin": ts, "ipPair": ipPair, "currLow": currLow,"currHigh": currHi,
                        "refHigh": ref["high"], "ref":ref["mean"], "refLow":ref["low"], 
                        "median": med, "nbSamples": n, "nbProbes": nbProbes, "deviation": deviation,
                        "diff": diff, "expId": expId, "diffMed": diffMed}

                if not collection is None:
                    alarms.append(alarm)
            
        # update past data
        if ipPair not in smoothMean: 
            smoothMean[ipPair] = {"mean": float(med), "high": float(currHi), 
                    "low": float(currLow), "probe": set(data["probe"])}  
        else:
            smoothMean[ipPair]["mean"] = (1-alpha)*smoothMean[ipPair]["mean"]+alpha*med
            smoothMean[ipPair]["high"] = (1-alpha)*smoothMean[ipPair]["high"]+alpha*currHi
            smoothMean[ipPair]["low"] = (1-alpha)*smoothMean[ipPair]["low"]+alpha*currLow
            smoothMean[ipPair]["probe"].update(data["probe"]) 


    # Insert all alarms to the database
    if len(alarms) and not collection is None:
        collection.insert_many(alarms)


def detectRttChangesMongo(configFile="detection.cfg"):

    nbProcesses = 6
    binMult = 5
    pool = Pool(nbProcesses,initializer=processInit) #, maxtasksperchild=binMult)

    # TODO clean this:
    metrics = [np.median, np.median, tools.mad] 

    expParam = {
            "timeWindow": 60*60, # in seconds 
            # "historySize": 24*7,  # 7 days
            "start": datetime(2015, 11, 15, 23, 45, tzinfo=timezone("UTC")), 
            "end":   datetime(2015, 12, 7, 0, 0, tzinfo=timezone("UTC")),
            "alpha": 0.01, 
            "confInterval": 0.05,
            "metrics": str(metrics),
            "minProbes": 5,
            "experimentDate": datetime.now(),
            "collection": "traceroute_2015_12", #TODO implement that part
            "comment": "analyze links only visited by a certain number of probes",
            }

    client = pymongo.MongoClient("mongodb-iijlab")
    db = client.atlas
    detectionExperiments = db.rttExperiments
    alarmsCollection = db.rttChanges
    expId = detectionExperiments.insert_one(expParam).inserted_id 

    sampleMedianMeasured = None 
    sampleMedianInferred = {}

    start = int(calendar.timegm(expParam["start"].timetuple()))
    end = int(calendar.timegm(expParam["end"].timetuple()))
    expParam["metrics"] = metrics

    for currDate in range(start,end,expParam["timeWindow"]):
        sys.stderr.write("Rtt analysis %s" % datetime.fromtimestamp(currDate))
        tsS = time.time()

        # Get distributions for the current time bin
        params = []
        binEdges = np.linspace(currDate, currDate+expParam["timeWindow"], nbProcesses*binMult+1)
        for i in range(nbProcesses*binMult):
            params.append( (binEdges[i], binEdges[i+1]) )

        measuredRtt = None
        inferredRtt = defaultdict(dict)
        nbRow = 0 
        rttResults =  pool.imap_unordered(computeRtt, params)
        measuredRtt, inferredRtt, nbRow = mergeRttResults(rttResults)

        # Detect oulier values
        for dist, smoothMean in [(measuredRtt, sampleMedianMeasured),
                (inferredRtt, sampleMedianInferred)]:
            outlierDetection(dist, smoothMean, expParam, expId, 
                    datetime.fromtimestamp(currDate), alarmsCollection)

        timeSpent = (time.time()-tsS)
        sys.stderr.write(", %s sec/bin,  %s row/sec\r" % (timeSpent, float(nbRow)/timeSpent))
    
    sys.stderr.write("\n")
    pool.close()
    pool.join()

    for ref, label in [(sampleMedianMeasured, "measured"), (sampleMedianInferred, "inferred")]:
        if not ref is None:
            print "Writing %s reference to file system." % (label)
            fi = open("saved_references/%s_%s.pickle" % (expId, label), "w")
            pickle.dump(ref, fi, 2) 

if __name__ == "__main__":
    # testDateRangeMongo(None,save_to_file=True)
    detectRttChangesMongo()



#### UNUSED FUNCTIONS #############

def testDateRangeFS(g,start = datetime(2015, 5, 10, 23, 45), 
        end = datetime(2015, 5, 12, 23, 45), msmIDs = range(5001,5027)):

    timeWindow = timedelta(minutes=30)
    stats = {"measured":defaultdict(list), "inferred": defaultdict(list)}
    meanRttMeasured = defaultdict(list)
    nbSamplesMeasured = defaultdict(list)
    meanRttInferred = defaultdict(list)
    nbSamplesInferred = defaultdict(list)

    currDate = start
    while currDate+timeWindow<end:
        rttMeasured = defaultdict(list)
        rttInferred = defaultdict(list)
        sys.stderr.write("\rTesting %s " % currDate)

        for i, msmId in enumerate(msmIDs):

            if not os.path.exists("../data/%s_msmId%s.json" % (currDate, msmId)):
                continue

            fi = open("../data/%s_msmId%s.json" % (currDate, msmId) )
            data = json.load(fi)

            for trace in data:
                readOneTraceroute(trace, rttMeasured, rttInferred)

        for k, v in rttMeasured.iteritems():
            meanRttMeasured[k].append(np.median(v))
            nbSamplesMeasured[k].append(len(v))
        for k, v in rttInferred.iteritems():
            meanRttInferred[k].append(np.median(v))
            nbSamplesInferred[k].append(len(v))
            

        currDate += timeWindow
    
    sys.stderr.write("\n")
    return meanRttMeasured, meanRttInferred, nbSamplesMeasured, nbSamplesInferred


def getMedianSamplesMongo(start = datetime(2015, 6, 7, 23, 45), 
        end = datetime(2015, 6, 13, 23, 59), msmIDs = range(5001,5027),save_to_file=False):

    nbProcesses = 6
    binMult = 5 
    pool = Pool(nbProcesses,initializer=processInit) #, maxtasksperchild=binMult)

    timeWindow = 30*60  # 30 minutes
    medianRttMeasured = defaultdict(list)
    nbSamplesMeasured = defaultdict(list)
    medianRttInferred = defaultdict(list)
    nbSamplesInferred = defaultdict(list)


    start = int(calendar.timegm(start.timetuple()))
    end = int(calendar.timegm(end.timetuple()))

    for currDate in range(start,end,timeWindow):
        sys.stderr.write("Analyzing %s " % currDate)
        tsS = time.time()

        params = []
        binEdges = np.linspace(currDate, currDate+timeWindow, nbProcesses*binMult+1)
        for i in range(nbProcesses*binMult):
            params.append( (binEdges[i], binEdges[i+1]) )

        measuredRtt = defaultdict(list)
        inferredRtt = defaultdict(list)
        nbRow = 0 
        rttResults =  pool.imap_unordered(computeRtt, params)
        measuredRtt, inferredRtt, nbRow = mergeRttResults(rttResults)

        # Computing samples median
        for k, v in measuredRtt.iteritems():
            medianRttMeasured[k].append(np.median(v))
            nbSamplesMeasured[k].append(len(v))
        for k, v in inferredRtt.iteritems():
            medianRttInferred[k].append(np.median(v))
            nbSamplesInferred[k].append(len(v))

        timeSpent = (time.time()-tsS)
        sys.stderr.write("Done in %s seconds,  %s row/sec\n" % (timeSpent, float(nbRow)/timeSpent))
        # readOneTraceroute(trace, rttMeasured, rttInferred)
    
    sys.stderr.write("\n")
    pool.close()
    pool.join()
    
    result = (medianRttMeasured, medianRttInferred, nbSamplesMeasured, nbSamplesInferred)
    if save_to_file:
        fp = open("test_1day.json","w")
        json.dump(result, fp)

    return result 

