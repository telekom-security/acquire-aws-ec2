#!/usr/bin/python3
# coding: utf-8
# -*- coding: utf-8 -*-


#
#
# Date:        08.11.2021
# Version:     1.1
# Author:      Simon Jansen. GitHub: https://github.com/0x534a
#
# Description:
#      Script used to acquire (multiple) AWS EC2 instances


import sys
import traceback
import logging
import argparse
import requests
from requests import RequestException
import boto3
from botocore.errorfactory import ClientError
import json
from datetime import datetime
import re
import string
import os
import subprocess
import shlex
import time
import multiprocessing
from multiprocessing import Pool, cpu_count, Lock
import multiprocessing_logging
from itertools import repeat
import platform


__version__ = "1.0"

# Global variables
LOGGER = None
AWS_SESSION = None
LOCK = Lock()


def main(argv):
    """
    Main entry point of the script
    """
    # Header
    print('''

                           _                        ___
   ____ __________ ___  __(_)_______      ___  ____|__ \\
  / __ `/ ___/ __ `/ / / / / ___/ _ \    / _ \/ ___/_/ /
 / /_/ / /__/ /_/ / /_/ / / /  /  __/   /  __/ /__/ __/
 \__,_/\___/\__, /\__,_/_/_/   \___/____\___/\___/____/
              /_/                 /_____/
        ''')
    print(" Ver. {}".format(__version__))
    print("")
    time.sleep(1)

    # Script needs ot be run as root to be able to image volume with dd
    if not os.geteuid() == 0:
        sys.exit("\nPlease run this script as root\n")
    try:
        # Global variables
        global LOGGER
        global AWS_SESSION
        # Argument handling
        args = handle_parameters(argv)
        # Initialize logging
        log_dir = "/var/log/acquire_ec2"
        if not os.path.isdir(log_dir):
            os.mkdir(log_dir)
        log_file = "{}_acquire_ec2_run_{}.log".format(datetime.now().strftime("%d-%m-%Y_%H-%M-%S"), args.case)
        log_path = os.path.join(log_dir, log_file)
        set_up_logging(log_path)
        LOGGER = logging.getLogger()
        LOGGER.info("Start of EC2 acquisition run for case {}".format(args.case))
        LOGGER.info("Results of this acquisition will be written to S3 bucket {}".format(args.s3_bucket))
        # Get own instance id
        instance_info = get_instance_info()
        region = instance_info["region"]
        own_instance_id = instance_info["instanceId"]
        LOGGER.info("Discovered own instance ID {} (region {})".format(own_instance_id, region))
        # Read list of EC2 instance IDs to acquire
        ec2_ids = read_instance_id_file(args.instance_list)
        LOGGER.info("Found {} EC2 instance IDs to acquire in input file {}".format(
            len(ec2_ids), args.instance_list.name))
        # Connect to AWS
        aws_connect(region, access_key=args.akey, secret_key=args.skey)
        ec2_session = get_ec2_session()
        LOGGER.info("EC2 session successfully created")
        s3_session = get_s3_session()
        LOGGER.info("S3 session successfully created")
        # Prepare S3 Bucket
        s3_base_dir = prepare_s3_bucket(args.case, args.s3_bucket, s3_session)
        # Iterate over list of EC2 instance IDs and discover volumes
        volumes = []
        for ec2_id in ec2_ids:
            # Gather EC2 instance information
            full_ec2_info = get_ec2_info_by_id(ec2_session, ec2_id)
            ec2_info = reduce_ec2_info(full_ec2_info)
            # Prepare S3 bucket and write instance information
            ec2_folder = "ec2_{}_{}".format(ec2_info["instance_id"], ec2_info["instance_name"])
            s3_ec2_path = create_s3_folder(args.s3_bucket, s3_session, s3_base_dir, ec2_folder)
            LOGGER.info("Created EC2 folder {} in S3 bucket {}".format(s3_ec2_path, args.s3_bucket))
            write_instance_info(args.s3_bucket, s3_session, s3_ec2_path, ec2_info["instance_id"], full_ec2_info)
            LOGGER.info("Dumped instance information as JSON file to EC2 folder {}".format(s3_ec2_path))
            # Prepare volume list
            for vol in ec2_info["volumes"]:
                vol = {
                    "volume_id": vol["volume_id"],
                    "device_name": vol["device_name"],
                    "instance_id": ec2_info["instance_id"],
                    "instance_name": ec2_info["instance_name"],
                    "region": region,
                    "availability_zone": ec2_info["availability_zone"],
                    "s3_path": s3_ec2_path,
                }
                volumes.append(vol)
        LOGGER.info("Discovered {} volumes attached to {} instances for acquisition".format(len(volumes), len(ec2_ids)))
        # ---- Multiprocessing ----
        # Discovered volumes will be acquired in multiple processes
        num_of_processes, proc_pool = get_mp_pool()
        LOGGER.info("Instantiated pool of {} workers to acquire volumes".format(num_of_processes))
        LOGGER.info("Starting acquisition workers...")
        proc_pool.starmap(
            acquire_ebs_volume,
            zip(
                volumes,
                repeat(own_instance_id),
                repeat(args.s3_bucket),
                repeat(args.kms_key),
            )
        )
        LOGGER.info("End of EC2 acquisition run for case {}".format(args.case))
        # Flush log
        logging.shutdown()
        # Copy log file to bucket
        copy_log(args.s3_bucket, s3_session, s3_base_dir, log_path, log_file)
    except Exception as ex:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        st = traceback.format_exception(exc_type, exc_value, exc_traceback,
                                        limit=4)
        if LOGGER:
            LOGGER.error(
                "A fatal error occurred. Error message was: {0} (stack trace: {1}). Exiting.".format(
                    ex, st))
        else:
            print("A fatal error occurred. Error message was: {0} (stack trace: {1}). Exiting.".format(
                    ex, st))
        sys.exit(1)


# <editor-fold desc="General helper functions">
def set_up_logging(log_file):
    """
    Sets up the logging infrastructure
    :param log_file: log file path
    """
    try:
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        log_formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] [PID: %(process)d]: %(message)s")
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(log_formatter)
        root_logger.addHandler(console_handler)
        if log_file:
            file_handler = logging.FileHandler(log_file, mode="a+", encoding="utf8")
            file_handler.setFormatter(log_formatter)
            root_logger.addHandler(file_handler)
        # Multiprocessing
        multiprocessing_logging.install_mp_handler()
    except Exception as err:
        raise err


def handle_parameters(argv):
    """
    Handles the script parameters
    :param argv: Script arguments
    :return: Parsed arguments
    """
    parser = argparse.ArgumentParser(
        description="Script for backing up network devices")
    # General script parameters
    parser.add_argument("--case",
                        help="Case name (no whitespaces allowed)",
                        type=case_arg,
                        required=True)
    parser.add_argument("--instance-list",
                        help="List of EC2 instance IDs to acquire",
                        type=argparse.FileType('r'),
                        required=True)
    parser.add_argument("--s3-bucket",
                        help="S3 bucket used to store forensic images",
                        required=True)
    parser.add_argument("--akey",
                        help="AWS access key ID")
    parser.add_argument("--skey",
                        help="AWS secret access key")
    parser.add_argument("--kms-key",
                        help="AWS KMS key ID")
    args = parser.parse_args()
    return args


def case_arg(value):
    """
    Type check for case argument
    :param value: argument value
    :return: value if type check was successful
    """
    if re.search(r'\s+', value):
        raise argparse.ArgumentTypeError("Case name should not contain whitespaces")
    return value


def read_instance_id_file(f):
    """
    Read the instance ID file
    :param f: file handle of instance ID file
    :return: list of instance IDs
    """
    instance_ids = []
    for line in f:
        instance_ids.append(line.strip())
    return instance_ids


def get_mp_pool(num_of_workers=None):
    """
    Create the multiprocessing worker pool
    :param num_of_workers: number of worker to spawn (default cpu_count-1)
    :return: tuple consisting of number and workers and the worker pool
    """
    if not num_of_workers:
        num_of_workers = cpu_count() - 1
        if num_of_workers < 1:
            num_of_workers = 1
    proc_pool = Pool(processes=num_of_workers, maxtasksperchild=1000)
    return num_of_workers, proc_pool


def get_next_available_device(session, ec2_id):
    """
    Returns the next available device beginning from /dev/sdg
    :param session: boto EC2 session
    :param ec2_id: EC2 instance ID
    :return: next available device beginning from /dev/sdg
    """
    attached_devs = get_attached_ebs_volumes(session, ec2_id)
    # Leave a little room for other volumes to attach
    for c in string.ascii_lowercase[6:]:
        dev_path = "/dev/sd" + c
        if dev_path not in attached_devs:
            return dev_path
    return None


def get_local_block_devices():
    devs = list()
    command = "lsblk --json"
    json_str = run_command(command)
    json_obj = json.loads(json_str)
    for dev in json_obj["blockdevices"]:
        if dev["type"] == "disk":
            devs.append("/dev/{}".format(dev["name"]))
    return devs


def run_command(command):
    """
    Runs the given command as subprocess
    :param command: command to run
    :return: stdout of run command
    """
    p = subprocess.Popen(
        shlex.split(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    stdout = p.communicate()
    if stdout[0]:
        return "{}".format(stdout[0].decode(encoding='ascii', errors='ignore').strip())
    return None


def get_vol_size(device):
    """
    Returns the size of a volume in bytes
    :param device: device path (e.g. /dev/sda)
    :return: size of a volume in bytes
    """
    command = "blockdev --getsize64 {}".format(device)
    size_str = run_command(command)
    if not size_str:
        raise Exception("Could not get size of block device {}. Device is probably not available.".format(device))
    return int(size_str)


def calculate_gp3_iops(vol_size_gib):
    """
    Calculates the optimal IOPS value for a certain gp3 volume size in GiB
    :param vol_size_gib: Size of EBS gp3 volume in GiB
    :return: IOPS value
    """
    # Define min and max IOPS to reduce cost
    min_iops = 3000
    max_iops = 10000
    # gp3 allows 500 IOPS per GiB
    iops = int(vol_size_gib * 500)
    # Check whether calculated value exceeds boundaries
    if iops > max_iops:
        iops = max_iops
    if iops < min_iops:
        iops = 3000
    return iops
# </editor-fold>


# <editor-fold desc="AWS Interaction">
def get_instance_info():
    """
    Requests the own instance information from the AWS metadata service
    :return: Own instance information as dict
    """
    try:
        r = requests.get("http://169.254.169.254/latest/dynamic/instance-identity/document")
        r.raise_for_status()
    except RequestException as e:
        LOGGER.error("Could not get own instance information from metadata service. Reason: {}".format(e))
        raise e
    try:
        response_json = r.json()
    except ValueError as e:
        LOGGER.error("Could not parse own instance information to JSON object. Reason: {}".format(e))
        raise e
    if "region" not in response_json or "instanceId" not in response_json:
        raise Exception("Could not fetch region and instance ID from instance information")
    return response_json


def aws_connect(region, access_key=None, secret_key=None):
    """
    Establishes connection to AWS API
    :param region: AWS region
    :param access_key: AWS access key ID
    :param secret_key: AWS secret access key
    """
    # Connect directly to Metadata service
    global AWS_SESSION
    try:
        if access_key and secret_key:
            AWS_SESSION = boto3.Session(
                region_name=region,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key
            )
        else:
            AWS_SESSION = boto3.Session(region_name=region)
    except Exception as e:
        LOGGER.error("Could not set up boto3 EC2 session. Reason: {}".format(e))
        raise e


def get_ec2_session():
    """
    Returns a boto EC2 session
    :return: boto EC2 session
    """
    client = None
    try:
        client = AWS_SESSION.client("ec2")
    except Exception as e:
        LOGGER.error("Could not set up boto3 EC2 session. Reason: {}".format(e))
    return client


def get_s3_session():
    """
    Returns a boto S3 session
    :return: boto S3 session
    """
    client = None
    try:
        client = AWS_SESSION.client("s3")
    except Exception as e:
        LOGGER.error("Could not set up boto3 S3 session. Reason: {}".format(e))
    return client


def get_ec2_info_by_id(session, ec2_id):
    """
    Returns the instance information of the EC2 instance defined by instance_id
    :param session: boto EC2 session
    :param ec2_id: EC2 instance ID
    :return: EC2 instance information as dict
    """
    response = session.describe_instances(InstanceIds=[ec2_id])
    status_code = response['ResponseMetadata']['HTTPStatusCode']
    if status_code != 200:
        raise Exception("Could not get EC2 information. HTTP return code was {}.".format(status_code))
    return response


def reduce_ec2_info(ec2_info):
    """
    Reduces the EC2 instance information
    :param ec2_info: EC2 instance information dict
    :return: reduced EC2 instance information as dict
    """
    # Ensure that ec2_info has the needed attributes
    if not len(ec2_info["Reservations"]):
        raise Exception("EC2 information does not contain needed attributes. Probably the instance is not running "
                        "or is not existing.")
    # Reduce the full EC2 information to only relevant attributes
    extracted_info = {
        "instance_id": ec2_info["Reservations"][0]["Instances"][0]["InstanceId"],
        "availability_zone": ec2_info["Reservations"][0]["Instances"][0]["Placement"]["AvailabilityZone"],
    }
    # Get instance name from tags
    tags = ec2_info["Reservations"][0]["Instances"][0]["Tags"]
    for tag in tags:
        if tag["Key"] == "Name":
            extracted_info["instance_name"] = tag["Value"]
    # Extract volumes
    extracted_info["volumes"] = []
    vols = ec2_info["Reservations"][0]["Instances"][0]["BlockDeviceMappings"]
    for vol in vols:
        vol_info = {
            "device_name": vol["DeviceName"],
            "volume_id": vol["Ebs"]["VolumeId"],
        }
        extracted_info["volumes"].append(vol_info)
    return extracted_info


def get_ebs_volume_size(session, volume_id):
    """
    Returns the EBS volume size in GiB
    :param session: boto EC2 session
    :param volume_id: EBS volume ID
    :return: size of EBS volume in GiB
    """
    response = session.describe_volumes(VolumeIds=[volume_id])
    status_code = response['ResponseMetadata']['HTTPStatusCode']
    if status_code != 200:
        raise Exception("Could not get EBS volumes information. HTTP return code was {}.".format(status_code))
    return response["Volumes"][0]["Size"]


def get_attached_ebs_volumes(session, ec2_id):
    """
    Returns a list of devices names of EBS volumes attached to the EC2 instance defined by instance_id
    :param session: boto EC2 session
    :param ec2_id: EC2 instance ID
    :return: EBS volume devices
    """
    ec2_info = get_ec2_info_by_id(session, ec2_id)
    vol_devices = list()
    vols = ec2_info["Reservations"][0]["Instances"][0]["BlockDeviceMappings"]
    for vol in vols:
        vol_device = vol["DeviceName"]
        vol_devices.append(vol_device)
    return vol_devices


def prepare_s3_bucket(case, s3_bucket, s3_session):
    """
    Prepares the S3 bucket for acquisition
    :param case: case name
    :param s3_bucket: S3 bucket name
    :param s3_session: boto S3 session
    :return: base directory path created for acquisition run
    """
    # Create acquisition directory
    s3_base_dir = "ec2_acquisition_{}_{}/".format(case, datetime.now().strftime("%d-%m-%Y_%H-%M-%S"))
    response = s3_session.put_object(Bucket=s3_bucket, Key=(s3_base_dir + '/'))
    status_code = response['ResponseMetadata']['HTTPStatusCode']
    if status_code != 200:
        raise Exception("Could not prepare S3 bucket. HTTP return code was {}.".format(status_code))
    return s3_base_dir


def create_s3_folder(s3_bucket, s3_session, base_dir, folder_name):
    """
    Creates the given S3 folder
    :param s3_bucket: S3 bucket name
    :param s3_session: boto S3 session
    :param base_dir: S3 bucket base directory
    :param folder_name: name of folder to create
    :return: S3 path of the created folder
    """
    if not base_dir.endswith("/"):
        base_dir += "/"
    s3_dir = base_dir + folder_name + "/"
    response = s3_session.put_object(Bucket=s3_bucket, Key=(s3_dir))
    status_code = response['ResponseMetadata']['HTTPStatusCode']
    if status_code != 200:
        raise Exception("Could create folder in S3 bucket. HTTP return code was {}.".format(status_code))
    return s3_dir


def copy_log(s3_bucket, s3_session, s3_path, log_path, log_file):
    """
    Copies the acquisition log to S3 bucket
    :param s3_bucket: S3 bucket name
    :param s3_session: boto S3 session
    :param s3_path: target directory path for log file in S3 bucket
    :param log_path: local log file path
    :param log_file: local log file name
    """
    s3_log_file_path = s3_path + log_file
    s3_session.upload_file(log_path, s3_bucket, s3_log_file_path)


def write_instance_info(s3_bucket, s3_session, instance_dir, instance_id, instance_info):
    """
    Writes the EC2 instance information as json file to S3 bucket
    :param s3_bucket: S3 bucket name
    :param s3_session: boto S3 session
    :param instance_dir: S3 instance folder
    :param instance_id: EC2 instance ID
    :param instance_info: EC2 instance information dict
    """
    if not instance_dir.endswith("/"):
        instance_dir += "/"
    file_path = instance_dir + "instance_info_{}.json".format(instance_id)
    content = json.dumps(instance_info, indent=4, sort_keys=True, default=str)
    response = s3_session.put_object(Bucket=s3_bucket, Key=(file_path), Body=content)
    status_code = response['ResponseMetadata']['HTTPStatusCode']
    if status_code != 200:
        raise Exception("Could not write instance information to S3 bucket. HTTP return code was {}.".format(
            status_code))


def write_volume_info(s3_bucket, s3_session, img_path, volume_info):
    """
    Writes volume information as json file to S3 bucket
    :param s3_bucket: S3 bucket name
    :param s3_session: boto S3 session
    :param img_path: volume image path
    :param volume_info: volume information dict
    :return: file path of volume information file
    """
    file_path = img_path + ".json"
    content = json.dumps(volume_info, indent=4, sort_keys=True, default=str)
    response = s3_session.put_object(Bucket=s3_bucket, Key=(file_path), Body=content)
    status_code = response['ResponseMetadata']['HTTPStatusCode']
    if status_code != 200:
        raise Exception("Could not write volume information to S3 bucket. HTTP return code was {}.".format(status_code))
    return file_path


def create_vol_snapshot(ec2_session, volume_id, instance_id):
    """
    Creates EBS volume snapshot
    :param ec2_session: boto EC2 session
    :param volume_id: EBS volume ID
    :param instance_id: EC2 instance ID
    :return: snapshot ID
    """
    try:
        tag_specifications = [
            {
                'ResourceType': 'snapshot',
                'Tags': [
                    {
                        'Key': 'Owner',
                        'Value': 'acquire_ec2'
                    },
                    {
                        'Key': 'Name',
                        'Value': "acquire_ec2_{}".format(volume_id)
                    }
                ]
            },
        ]
        response = ec2_session.create_snapshot(
            Description="Created by acquire_ec2 for forensic acquisition of instance {}".format(instance_id),
            VolumeId=volume_id,
            TagSpecifications=tag_specifications,
            DryRun=False
        )
        # response is a dictionary containing ResponseMetadata and SnapshotId
        status_code = response['ResponseMetadata']['HTTPStatusCode']
        snapshot_id = response['SnapshotId']
        # check if status_code was 200 or not to ensure the snapshot was created successfully
        if status_code != 200:
            raise Exception("Could not create volume snapshot. HTTP return code was {}.".format(status_code))
        # Block function until snapshot is created
        ec2_session.get_waiter('snapshot_completed').wait(
            SnapshotIds=[snapshot_id]
        )
    except Exception as e:
        LOGGER.error("Snapshot for volume {} could not be created. Error message was: {}.".format(volume_id, e))
        raise e
    return snapshot_id


def create_vol_from_snapshot(ec2_session, volume_id, volume_size, snap_id, availability_zone, kms_key_id):
    """
    Creates EBS volume based on snapshot
    :param ec2_session: boto EC2 session
    :param volume_id: EBS volume ID
    :param volume_size: EBS volume size in GiB
    :param snap_id: snapshot ID
    :param availability_zone: AWS availability zone
    :param kms_key_id: AWS KMS key ID that should be used for encryption
    :return: temporary EBS volume ID
    """
    try:
        tag_specifications = [
            {
                'ResourceType': 'volume',
                'Tags': [
                    {
                        'Key': 'Owner',
                        'Value': 'acquire_ec2'
                    },
                    {
                        'Key': 'Name',
                        'Value': "acquire_ec2_{}".format(volume_id)
                    }
                ]
            },
        ]
        iops = calculate_gp3_iops(volume_size)
        if kms_key_id:
            response = ec2_session.create_volume(
                AvailabilityZone=availability_zone,
                SnapshotId=snap_id,
                VolumeType='gp3',
                Iops=iops,
                TagSpecifications=tag_specifications,
                Throughput=500,
                Encrypted=True,
                KmsKeyId=kms_key_id,
            )
        else:
            response = ec2_session.create_volume(
                AvailabilityZone=availability_zone,
                SnapshotId=snap_id,
                VolumeType='gp3',
                Iops=iops,
                TagSpecifications=tag_specifications,
                Throughput=500,
            )
        # response is a dictionary containing ResponseMetadata and VolumeId
        status_code = response['ResponseMetadata']['HTTPStatusCode']
        vol_id = response['VolumeId']
        # check if status_code was 200 or not to ensure the volume was created successfully
        if status_code != 200:
            raise Exception("Could not create volume based on snapshot. HTTP return code was {}.".format(status_code))
        # Block function until volume is created
        ec2_session.get_waiter('volume_available').wait(
            VolumeIds=[vol_id]
        )
    except Exception as e:
        LOGGER.error("Volume for snapshot {} could not be created. Error message was: {}.".format(snap_id, e))
        raise e
    return vol_id


def attach_volume(ec2_session, instance_id, volume_id, device):
    """
    Attaches volume to EC2 instance
    :param ec2_session: boto EC2 session
    :param instance_id: EC2 instance ID
    :param volume_id: EBS volume ID
    :param device: device name to attach volume to (e.g. /dev/sdg)
    """
    try:
        # To determine the newly attached device compare the device list before and after attaching the new volume
        # This is needed since the block device in the OS differs from the block device defined while attaching the
        # volume in AWS
        devs_before = get_local_block_devices()
        response = ec2_session.attach_volume(
            VolumeId=volume_id,
            Device=device,
            InstanceId=instance_id
        )
        # response is a dictionary containing ResponseMetadata
        status_code = response['ResponseMetadata']['HTTPStatusCode']
        # check if status_code was 200 or not to ensure the volume was created successfully
        if status_code != 200:
            raise Exception("Could not attach volume. HTTP return code was {}.".format(status_code))
        # Block function until volume is attached
        ec2_session.get_waiter('volume_in_use').wait(
            VolumeIds=[volume_id]
        )
        # Wait another five seconds to ensure that the volume is available in the OS
        time.sleep(5)
        devs_after = get_local_block_devices()
        newly_attached_dev = list(set(devs_after) - set(devs_before))[0]
        return newly_attached_dev
    except Exception as e:
        LOGGER.error("Volume {} could not be attached to EC2 instance {}. Error message was: {}.".format(
            volume_id, instance_id, e))
        raise e


def detach_volume(ec2_session, volume_id):
    """
    Detaches volume from EC2 instance
    :param ec2_session: boto EC2 session
    :param volume_id: EBS volume ID to detach
    """
    try:
        response = ec2_session.detach_volume(
            VolumeId=volume_id
        )
        # response is a dictionary containing ResponseMetadata
        status_code = response['ResponseMetadata']['HTTPStatusCode']
        # check if status_code was 200 or not to ensure the volume was created successfully
        if status_code != 200:
            raise Exception("Could not detach volume. HTTP return code was {}.".format(status_code))
        # Block function until volume is attached
        ec2_session.get_waiter('volume_available').wait(
            VolumeIds=[volume_id]
        )
    except Exception as e:
        LOGGER.error("Volume {} could not be detached from EC2 instance. Error message was: {}.".format(
            volume_id, e))
        raise e


def delete_volume(ec2_session, volume_id):
    """
    Deletes EBS volume
    :param ec2_session: boto EC2 session
    :param volume_id: EBS volume ID to delete
    """
    try:
        response = ec2_session.delete_volume(
            VolumeId=volume_id
        )
        # response is a dictionary containing ResponseMetadata
        status_code = response['ResponseMetadata']['HTTPStatusCode']
        # check if status_code was 200 or not to ensure the volume was created successfully
        if status_code != 200:
            raise Exception("Could not delete volume. HTTP return code was {}.".format(status_code))
        # Block function until volume is attached
        ec2_session.get_waiter('volume_deleted').wait(
            VolumeIds=[volume_id]
        )
    except Exception as e:
        LOGGER.error("Volume {} could not be deleted. Error message was: {}.".format(volume_id, e))
        raise e


def acquire_volume(device, device_size, region, s3_session, s3_bucket, s3_path, volume_id, instance_name, instance_id):
    """
    Acquires EBS volume to S3 bucket
    :param device: device name to acquire (e.g. /dev/sdg)
    :param device_size: size of volume in bytes
    :param region: AWS region
    :param s3_bucket: S3 bucket name
    :param s3_path: S3 path to store image in
    :param volume_id: EBS volume ID
    :param instance_name: EC2 instance name
    :param instance_id: EC2 instance ID
    :return: file path of the acquired EBS image
    """
    # By using a piped command we can directly write the result of dd to the S3 bucket
    # dd will write image data to stdout and aws cli will read the data from stdin
    dd_command = "dd if={} bs=65536 conv=noerror,sync".format(device)
    dd_p = subprocess.Popen(
        shlex.split(dd_command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    # AWS CLI s3 command will read data from stdin and write it to s3 bucket
    s3_file_path = "{}{}_{}_{}.dd".format(s3_path, volume_id, instance_name, instance_id)
    s3_uri = "s3://{}/{}".format(s3_bucket, s3_file_path)
    aws_command = "aws s3 cp --region {} --expected-size {} - {}".format(region, device_size, s3_uri)
    aws_p = subprocess.Popen(
        shlex.split(aws_command),
        stdin=dd_p.stdout,  # Pipe commands
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    stdout, stderr = aws_p.communicate()
    # If there is output on stdout or stderr we have an error
    if stdout or stderr:
        output = ""
        if stdout:
            output = stdout.decode(encoding='ascii', errors='ignore').strip()
        elif stderr:
            output = stderr.decode(encoding='ascii', errors='ignore').strip()
        raise Exception("Could not acquire device {} using dd. Output was: {}".format(device, output))
    # Make sure the image file exists in the bucket
    if not s3_file_exists(s3_session, s3_bucket, s3_file_path):
        raise Exception("Something went wrong during acquisition of device {} using dd. File {} not existing in S3 bucket {}.".format(
            device, s3_file_path, s3_bucket))
    return s3_file_path


def s3_file_exists(session, bucket, file_path):
    """
    Checks whether a file exists in an S3 bucket
    :param session: boto S3 session
    :param bucket: S3 bucket
    :param file_path: S3 file path
    :return: bool that indicates whether file exists in S3 bucket
    """
    try:
        session.head_object(Bucket=bucket, Key=file_path)
        return True
    except ClientError:
        return False


def get_image_hash(region, s3_bucket, s3_image_path):
    """
    Hashes acquired volume image
    :param region: AWS region
    :param s3_bucket: S3 bucket name
    :param s3_image_path: EBS volume image path
    :return: SHA-256 hash sum of EBS volume image
    """
    # By using a piped command we can directly write the result of dd to the S3 bucket
    # dd will write image data to stdout and aws cli will read the data from stdin
    # AWS CLI s3 command will read data from stdin and write it to s3 bucket
    s3_uri = "s3://{}/{}".format(s3_bucket, s3_image_path)
    aws_command = "aws s3 cp --region {} {} -".format(region, s3_uri)
    aws_p = subprocess.Popen(
        shlex.split(aws_command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    sha_command = "sha256sum"
    sha_p = subprocess.Popen(
        shlex.split(sha_command),
        stdin=aws_p.stdout,  # Pipe commands
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    stdout = sha_p.communicate()
    if stdout[0]:
        # output of the pipe command is the image hash
        output = "{}".format(stdout[0].decode(encoding='ascii', errors='ignore').strip())
        return output.split(" ")[0]
    return None
# </editor-fold>


# <editor-fold desc="Worker Process">
def acquire_ebs_volume(volume, acquire_instance_id, s3_bucket, kms_key_id):
    """
    Worker process to acquire EBS volume
    :param volume: EBS volume information
    :param acquire_instance_id: EC2 instance ID of acquisition instance
    :param s3_bucket: S3 bucket name
    :param kms_key_id: AWS KMS key ID that should be used for encryption
    """
    temp_volume_attached = False
    try:
        volume["start_ts"] = datetime.now().isoformat()
        current_process = multiprocessing.current_process()
        LOGGER.info("Acquiring volume {} (AWS instance ID {}) in forked process {} (PID: {})".format(
            volume["volume_id"],
            volume["instance_id"],
            current_process.name,
            current_process.pid,
        ))
        # Due to multiprocessing we need to reopen the boto sessions
        LOGGER.info("Reconnecting to AWS metadata service in forked process...")
        aws_connect(volume["region"])
        ec2_session = get_ec2_session()
        LOGGER.info("EC2 session successfully created")
        s3_session = get_s3_session()
        LOGGER.info("S3 session successfully created")
        # Create snapshot
        vol_id = volume["volume_id"]
        snap_id = create_vol_snapshot(ec2_session, vol_id, volume["instance_id"])
        LOGGER.info("Snapshot of volume {} successfully created (snapshot ID {})".format(vol_id, snap_id))
        volume["snapshot_id"] = snap_id
        # Create temp volume based on created snapshot
        volume["size"] = get_ebs_volume_size(ec2_session, vol_id)
        snap_vol_id = create_vol_from_snapshot(
            ec2_session,
            vol_id,
            volume["size"],
            snap_id,
            volume["availability_zone"],
            kms_key_id
        )
        if kms_key_id:
            LOGGER.info("Created temporary volume {} (encrypted by KMS key ID {}) based on snapshot {}".format(
                snap_vol_id, kms_key_id, snap_id))
        else:
            LOGGER.info("Created temporary volume {} based on snapshot {}".format(snap_vol_id, snap_id))
        # Attach volume to acquisition EC2 instance and acquire volume using dd
        # Image is directly written to S3 bucket
        # Run with mutex to avoid that volumes are attached to the same device
        with LOCK:
            dev = get_next_available_device(ec2_session, acquire_instance_id)
            os_dev = attach_volume(ec2_session, acquire_instance_id, snap_vol_id, dev)
            LOGGER.info("Attached temporary volume {} to EC2 instance as device {} (OS device {})".format(
                snap_vol_id, dev, os_dev))
            temp_volume_attached = True
        dev_size = get_vol_size(os_dev)
        LOGGER.info("Size of attached volume is {} bytes".format(dev_size))
        run_start = time.perf_counter()
        s3_img_path = acquire_volume(
            os_dev,
            dev_size,
            volume["region"],
            s3_session,
            s3_bucket,
            volume["s3_path"],
            vol_id,
            volume["instance_name"],
            volume["instance_id"],
        )
        run_end = time.perf_counter()
        runtime = run_end - run_start
        rate = (dev_size / (1024*1024)) / runtime
        LOGGER.info("Volume {} of instance {} acquired and written to S3 bucket {}".format(
            vol_id, volume["instance_id"], s3_img_path))
        LOGGER.info("Acquisition of volume {} took {:.4f} s ({:.4f} MB/s)".format(vol_id, runtime, rate))
        volume["acquisition_rate_mbs"] = rate
        # Calculate hash sum of volume
        run_start = time.perf_counter()
        hash_sum = get_image_hash(volume["region"], s3_bucket, s3_img_path)
        run_end = time.perf_counter()
        runtime = run_end - run_start
        rate = (dev_size / (1024 * 1024)) / runtime
        LOGGER.info("SHA256 acquisition hash of image file {} is {} (read rate {:.4f} MB/s)".format(
            s3_img_path, hash_sum, rate))
        # Add hash sum and hash rate to volume dict
        volume["sha256_hash"] = hash_sum
        volume["hash_rate_mbs"] = rate
        # Detach temp volume
        detach_volume(ec2_session, snap_vol_id)
        LOGGER.info("Detached temporary volume {} from EC2 instance".format(snap_vol_id))
        # Delete temp volume
        delete_volume(ec2_session, snap_vol_id)
        LOGGER.info("Deleted temporary volume {}".format(snap_vol_id))
        volume["end_ts"] = datetime.now().isoformat()
        # Write acquisition information to file
        volume_info_path = write_volume_info(s3_bucket, s3_session, s3_img_path, volume)
        LOGGER.info("Volume acquisition information written to file {}".format(volume_info_path))
    except Exception as ex:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        st = traceback.format_exception(exc_type, exc_value, exc_traceback,
                                        limit=4)
        LOGGER.error(
            "Could not acquire volume {} (instance {}). Error message was: {} (stack trace: {}). Exiting.".format(
                volume["volume_id"], volume["instance_id"], ex, st))
        if temp_volume_attached:
            # Detach temp volume
            detach_volume(ec2_session, snap_vol_id)
            LOGGER.info("Detached temporary volume {} from EC2 instance".format(snap_vol_id))
            # Delete temp volume
            delete_volume(ec2_session, snap_vol_id)
            LOGGER.info("Deleted temporary volume {}".format(snap_vol_id))
# </editor-fold>


if __name__ == '__main__':
    main(sys.argv[1:])
