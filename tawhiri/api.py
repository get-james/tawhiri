# Copyright 2014 (C) Priyesh Patel
#
# This file is part of Tawhiri.
#
# Tawhiri is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Tawhiri is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Tawhiri.  If not, see <http://www.gnu.org/licenses/>.

"""
Provide the HTTP API for Tawhiri.
"""

from flask import Flask, jsonify, request, g
from datetime import datetime
import time
import strict_rfc3339

from tawhiri import solver, models
from tawhiri.dataset import Dataset as WindDataset
from tawhiri.warnings import WarningCounts
from ruaumoko import Dataset as ElevationDataset

app = Flask(__name__)

API_VERSION = 1
LATEST_DATASET_KEYWORD = "latest"
PROFILE_STANDARD = "standard_profile"
PROFILE_FLOAT = "float_profile"


# Util functions ##############################################################
def ruaumoko_ds():
    if not hasattr("ruaumoko_ds", "once"):
        ds_loc = app.config.get('ELEVATION_DATASET', ElevationDataset.default_location)
        ruaumoko_ds.once = ElevationDataset(ds_loc)

    return ruaumoko_ds.once

def _rfc3339_to_timestamp(dt):
    """
    Convert from a RFC3339 timestamp to a UNIX timestamp.
    """
    return strict_rfc3339.rfc3339_to_timestamp(dt)

def _timestamp_to_rfc3339(dt):
    """
    Convert from a UNIX timestamp to a RFC3339 timestamp.
    """
    return strict_rfc3339.timestamp_to_rfc3339_utcoffset(dt)

def is_time_between(begin_time, end_time, dt_time):
    """
    used to convert conversion to nearest dataset time
    """
    return begin_time <= dt_time < end_time
       

def hour_to_nearest_dataset(day, hour):
    """
    returns nearest dataset time less than the requested hour
    """
    hours = [0,6,12,18]
    end_hours = [6,12,18,24]
    for i in range(4):
        if is_time_between(hours[i],end_hours[i], hour):
           if hours[i] == 0:
               day = day-1
           return day, hours[i]
# Exceptions ##################################################################
class APIException(Exception):
    """
    Base API exception.
    """
    status_code = 500


class RequestException(APIException):
    """
    Raised if request is invalid.
    """
    status_code = 400


class InvalidDatasetException(APIException):
    """
    Raised if the dataset specified in the request is invalid.
    """
    status_code = 404


class PredictionException(APIException):
    """
    Raised if the solver raises an exception.
    """
    status_code = 500


class InternalException(APIException):
    """
    Raised when an internal error occurs.
    """
    status_code = 500


class NotYetImplementedException(APIException):
    """
    Raised when the functionality has not yet been implemented.
    """
    status_code = 501


# Request #####################################################################
def parse_request(data):
    """
    Parse the request.
    """
    req = {"version": API_VERSION}

    # Generic fields
    req['launch_latitude'] = \
        _extract_parameter(data, "launch_latitude", float,
                           validator=lambda x: -90 <= x <= 90)
    req['launch_longitude'] = \
        _extract_parameter(data, "launch_longitude", float,
                           validator=lambda x: 0 <= x < 360)
    req['launch_datetime'] = \
        _extract_parameter(data, "launch_datetime", _rfc3339_to_timestamp)
    req['launch_altitude'] = \
        _extract_parameter(data, "launch_altitude", float, ignore=True)

    # If no launch altitude provided, use Ruaumoko to look it up
    if req['launch_altitude'] is None:
        try:
            req['launch_altitude'] = ruaumoko_ds().get(req['launch_latitude'],
                                                       req['launch_longitude'])
        except Exception:
            raise InternalException("Internal exception experienced whilst " +
                                    "looking up 'launch_altitude'.")

    # Prediction profile
    req['profile'] = _extract_parameter(data, "profile", str,
                                        PROFILE_STANDARD)

    launch_alt = req["launch_altitude"]

    if req['profile'] == PROFILE_STANDARD:
        req['ascent_rate'] = _extract_parameter(data, "ascent_rate", float,
                                                validator=lambda x: x > 0)
        req['burst_altitude'] = \
            _extract_parameter(data, "burst_altitude", float,
                               validator=lambda x: x > launch_alt)
        req['descent_rate'] = _extract_parameter(data, "descent_rate", float,
                                                 validator=lambda x: x > 0)
    elif req['profile'] == PROFILE_FLOAT:
        req['ascent_rate'] = _extract_parameter(data, "ascent_rate", float,
                                                validator=lambda x: x > 0)
        req['float_altitude'] = \
            _extract_parameter(data, "float_altitude", float,
                               validator=lambda x: x > launch_alt)
        req['stop_datetime'] = \
            _extract_parameter(data, "stop_datetime", _rfc3339_to_timestamp,
                               validator=lambda x: x > req['launch_datetime'])
    else:
        raise RequestException("Unknown profile '%s'." % req['profile'])

    # Dataset
    req['dataset'] = _extract_parameter(data, "dataset", _rfc3339_to_timestamp,
                                        LATEST_DATASET_KEYWORD)

    return req


def _extract_parameter(data, parameter, cast, default=None, ignore=False,
                       validator=None):
    """
    Extract a parameter from the POST request and raise an exception if any
    parameter is missing or invalid.
    """
    if parameter not in data:
        if default is None and not ignore:
            raise RequestException("Parameter '%s' not provided in request." %
                                   parameter)
        return default

    try:
        result = cast(data[parameter])
    except Exception:
        raise RequestException("Unable to parse parameter '%s': %s." %
                               (parameter, data[parameter]))

    if validator is not None and not validator(result):
        raise RequestException("Invalid value for parameter '%s': %s." %
                               (parameter, data[parameter]))

    return result

def _is_old_dataset(req):
    """
    if dataset name is found in the default directory ("tawhiri_datasets" folder)
    we still need to change req['dataset'] because we might still be working with an old file that is already downloaded by a previous run. so it's not ok to just use latest
    
    if it's not found then we're working with historical data. parse_request() by default will set req['dataset'] to LATEST_DATASET_KEYWORD if no dataset is passed
    so we change this parameter manually to make that not the case.
    """

    dataset_name =_date_to_dataset_name(req['launch_datetime'])

    
    #list dir yields a namedtuple with the suffix field which is the first 10 characters of a file name. aka the file name
    #
    for stuff in WindDataset.listdir():
        if dataset_name == stuff.filename:
            req['dataset'] = dataset_name#we might still not want to use latest.
            return False
        
    req['dataset'] = dataset_name
    return True
    
def _date_to_dataset_name(rcf_launch_time):
    """
    converts req["launch_datetime"] to string of YYYYMMDDHH since that's how the dataset files are named in "tawhiri_datasets"
    need to convert to nearest hours that data is collected
    """
    
    #need to be careful here to convert the hour to the closest dataset, 00, 06, 12, 18
    launch_time_stamp = _rfc3339_to_timestamp(rcf_launch_time)
    launch_date_time = datetime.fromtimestamp(launch_time_stamp)
    #changing hour to nearest dataset
    dataset_day, dataset_hour = hour_to_nearest_dataset(launch_date_time.day, launch_date_time.hour)
    launch_date_time = launch_date_time.replace(day = dataset_day, hour =dataset_hour)#replace does not work in place
    filename = launch_date_time.strftime("%Y%m%d%H")

    return filename

def _download_old_dataset(launch_datetime):
    #docker run tawhiri_download_container
    #or call a shell script that runs download container since that's how things have been working so far
    return


# Response ####################################################################
def run_prediction(req):
    """
    Run the prediction.
    """
    #run this first since it modifies response dict:
    if _is_old_dataset(req):
        _download_old_dataset(req['launch_datime'])

    # Response dict
    resp = {
        "request": req,
        "prediction": [],
    }

    warningcounts = WarningCounts()

    # Find wind data location
    ds_dir = app.config.get('WIND_DATASET_DIR', WindDataset.DEFAULT_DIRECTORY)
    """
    date_to_dataset_name(req['launch_datetime'])
    
    """
    # Dataset
    try:
        if req['dataset'] == LATEST_DATASET_KEYWORD:
            tawhiri_ds = WindDataset.open_latest(persistent=True, directory=ds_dir)
        else:
            tawhiri_ds = WindDataset(datetime.fromtimestamp(req['dataset']), directory=ds_dir)
    except IOError:
        raise InvalidDatasetException("No matching dataset found.")
    except ValueError as e:
        raise InvalidDatasetException(*e.args)

    # Note that hours and minutes are set to 00 as Tawhiri uses hourly datasets
    resp['request']['dataset'] = \
            tawhiri_ds.ds_time.strftime("%Y-%m-%dT%H:00:00Z")

    # Stages
    if req['profile'] == PROFILE_STANDARD:
        stages = models.standard_profile(req['ascent_rate'],
                                         req['burst_altitude'],
                                         req['descent_rate'],
                                         tawhiri_ds,
                                         ruaumoko_ds(),
                                         warningcounts)
    elif req['profile'] == PROFILE_FLOAT:
        stages = models.float_profile(req['ascent_rate'],
                                      req['float_altitude'],
                                      req['stop_datetime'],
                                      tawhiri_ds,
                                      warningcounts)
    else:
        raise InternalException("No implementation for known profile.")

    # Run solver
    try:
        result = solver.solve(req['launch_datetime'], req['launch_latitude'],
                              req['launch_longitude'], req['launch_altitude'],
                              stages)
    except Exception as e:
        """
        instead of raising exception
        run code here to run new docker container with modified version of download.py
        that keeps executing in the same place. code can just continue as is.
        include a wait and a timout mechanism
        """
        raise PredictionException("Prediction did not complete: '%s'." %
                                  str(e))

    # Format trajectory
    if req['profile'] == PROFILE_STANDARD:
        resp['prediction'] = _parse_stages(["ascent", "descent"], result)
    elif req['profile'] == PROFILE_FLOAT:
        resp['prediction'] = _parse_stages(["ascent", "float"], result)
    else:
        raise InternalException("No implementation for known profile.")

    # Convert request UNIX timestamps to RFC3339 timestamps
    for key in resp['request']:
        if "datetime" in key:
            resp['request'][key] = _timestamp_to_rfc3339(resp['request'][key])

    resp["warnings"] = warningcounts.to_dict()

    return resp


def _parse_stages(labels, data):
    """
    Parse the predictor output for a set of stages.
    """
    assert len(labels) == len(data)

    prediction = []
    for index, leg in enumerate(data):
        stage = {}
        stage['stage'] = labels[index]
        stage['trajectory'] = [{
            'latitude': lat,
            'longitude': lon,
            'altitude': alt,
            'datetime': _timestamp_to_rfc3339(dt),
            } for dt, lat, lon, alt in leg]
        prediction.append(stage)
    return prediction


# Flask App ###################################################################
@app.route('/api/v{0}/'.format(API_VERSION), methods=['GET'])
def main():
    """
    Single API endpoint which accepts GET requests.
    """
    g.request_start_time = time.time()
    response = run_prediction(parse_request(request.args))
    g.request_complete_time = time.time()
    response['metadata'] = _format_request_metadata()
    return jsonify(response)


@app.errorhandler(APIException)
def handle_exception(error):
    """
    Return correct error message and HTTP status code for API exceptions.
    """
    response = {}
    response['error'] = {
        "type": type(error).__name__,
        "description": str(error)
    }
    g.request_complete_time = time.time()
    response['metadata'] = _format_request_metadata()
    return jsonify(response), error.status_code


def _format_request_metadata():
    """
    Format the request metadata for inclusion in the response.
    """
    return {
        "start_datetime": _timestamp_to_rfc3339(g.request_start_time),
        "complete_datetime": _timestamp_to_rfc3339(g.request_complete_time),
    }
