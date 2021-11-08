# acquire_ec2.py
The script `acquire_ec2.py` is used to automatically acquire AWS EC2 instances. The script needs to be run on an EC2 instance in the same region as the EC2 instances that should be acquired. It was
developed for forensic acquisition if an analyst wants to apply traditional forensic analysis of AWS EC2 instances. It was developed for internal use by a former DT-Sec employee. No guarantees are made

## Acquisition Process
Since there is no export functionality available in neither the AWS console nor the AWS API, the acquisition of EC2 instances or more precisely their attached EBS volumes needs to be done manually.
This is the reason why this script was developed.
Basically, the script automatically discovers and images the EBS volumes attached to a list of EC2 instances identified by their EC2 instance ID.
The raw images files are written to an S3 bucket where they can be downloaded for further analysis.

For each of the discovered EBS volumes the following process is executed:
1. Create snapshot of EBS volume
1. Create temporary EBS volume based on created snapshot
1. Attach temporary EBS volume to acquisition host
1. Image attached EBS volume using `dd` and write image to S3 bucket
1. Hash volume image
1. Detach temporary EBS volume
1. Delete temporary EBS volume

The following diagram shows an overview of the acquisition process.

![acquire_ec2_architecture](/doc/20210520_acquire_ec2_overview.drawio.png)

## Prerequisites
### Acquisition Host and Dependencies
The script needs to be run as root on an EC2 instance in the same region as the EC2 instances that should be acquired.
The EC2 instance which is used for acquisition (acquisition host) should be sized as
* `r6g.xlarge` (Preferred, ARM-based, 10 Gbit network) or
* `r5n.xlarge` (x64-based, 25 Gbit network).

Since the script uses multiple processes to acquire EC2 instances, more CPU cores will lead to more acquisition processes that can be executed concurrently.
By default, `cpu_count - 1` processes are spawned. Hence, for large environments to acquire a larger instance with at least 8 CPU cores is beneficial.

The scripts needs to be run on an Linux based AMI (e.g. Amazon Linux or Ubuntu).
The following software / commands needs to be present on the acquisition host:
* `python3`
* `python3-pip`
* `aws cli`
* `dd`
* `blockdev`
* `sha256sum`

As for Amazon Linux, this leads to the following packages to be installed additionally:
* `python3`
* `python3-pip`

The packages can be installed by running the following command.

```
$ sudo yum install python3 python3-pip
```

As for Ubuntu, the following packages need to be installed:
* `awscli`
* `python3-pip`

Please consider that `awscli` should be installed using `pip3` since the package in the repository is broken.
The installation commands are as follows
```
$ sudo apt install python3-pip
$ sudo pip3 install awscli
```

In order to fulfill the Python dependencies of `acquire_ec2.py` the following pip packages need to be installed
* `boto3==1.17.5`
* `botocore==1.20.5`
* `certifi==2020.12.5`
* `chardet==4.0.0`
* `idna==2.10`
* `jmespath==0.10.0`
* `multiprocessing-logging==0.3.1`
* `python-dateutil==2.8.1`
* `requests==2.25.1`
* `s3transfer==0.3.4`
* `six==1.15.0`
* `urllib3==1.26.3`

To install the python dependencies run the following command.
```
$ sudo pip3 install -r requirements.txt
```

### S3 Bucket
To store the acquired volume images an S3 bucket is needed.
This destination S3 bucket needs to be created before running the script and needs to be accessible from the internet with a proper access and secret key with multi-factor authentication.
Furthermore this S3 bucket needs to encrypted using a custom KMS key.
Typically, this S3 bucket should be created by the personnel operating the AWS environment.

### Policy
To allow access to the relevant resources the following policy should be implemented.
```
{
    "Version": "2021-02-26",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "s3:PutObject",
                "s3:PutObjectTagging",
                "s3:GetObject"
            ],
            "Resource": [
                "arn:aws:s3:::<bucket-name>/*"
            ]
        },
        {
            "Effect": "Allow",
            "Action": [
                "ec2:CreateVolume",
                "ec2:AttachVolume",
                "ec2:DetachVolume",
                "ec2:DeleteVolume",
                "ec2:CreateSnapshot",
                "ec2:DescribeSnapshots",
                "ec2:DescribeVolumes",
                "ec2:DescribeInstanceAttribute",
                "ec2:DescribeInstances",
                "ec2:CreateTags",
                "ec2:DescribeTags",
                "ec2:DeleteTags"
            ],
            "Resource": [
                "*"
            ]
        }
    ]
}
```

The policy can either be attached to the acquisition host EC2 instance as instance role (tested) or to a specific IAM user (currently not tested).

### KMS Key Access
Since customer managed KMS keys should be used for the creation of volumes, access to the relevant KMS key needs to be granted to the instance IAM role or to the IAM user.

### Needed from AWS Operations Team
The following is needed from the AWS operations team in order to successfully acquire EBS volumes:
- EC2 acquisition host which has the access rights described in the policy section. The policy can be set up as instance role.
- S3 Bucket to store the acquired images. The bucket must follow the currently valid security requirements. The bucket needs to be accessible from the environment you want to download the data to (ip based).
- Access to the KMS key used by the application to encrypt EBS volumes.
- Access to the KMS key that should be used for the acquisition process.

## Future Work Ideas
- Set up a template (AMI) for the acquisition host
- Evaluate the usage of an independent forensics VPC that does not have to be set up for each incident in the application VPC

## Usage
The script can be used as follows.
```
[root@ip-192-168-28-243 ec2-user]# python3 acquire_ec2.py -h


                           _                        ___
   ____ __________ ___  __(_)_______      ___  ____|__ \
  / __ `/ ___/ __ `/ / / / / ___/ _ \    / _ \/ ___/_/ /
 / /_/ / /__/ /_/ / /_/ / / /  /  __/   /  __/ /__/ __/
 \__,_/\___/\__, /\__,_/_/_/   \___/____\___/\___/____/
              /_/                 /_____/

 Ver. 1.0

usage: acquire_ec2.py [-h] --case CASE --instance-list INSTANCE_LIST
                      --s3-bucket S3_BUCKET [--akey AKEY] [--skey SKEY]
                      [--kms-key KMS_KEY]

Script for backing up network devices

optional arguments:
  -h, --help            show this help message and exit
  --case CASE           Case name (no whitespaces allowed)
  --instance-list INSTANCE_LIST
                        List of EC2 instance IDs to acquire
  --s3-bucket S3_BUCKET
                        S3 bucket used to store forensic images
  --akey AKEY           AWS access key ID
  --skey SKEY           AWS secret access key
  --kms-key KMS_KEY     AWS KMS key ID
```

The following mandatory parameters need to be passed to the script:
* `case` (case name)
* `instance-list` (text file that contains the EC2 instance IDs to acquire)
* `s3-bucket` (S3 bucket name)

If no KMS key ID is passed, the standard encryption key is used. F
or productional use of the script, a customer managed KMS key should be passed.

For example the script can be called as follows.
```
$ sudo python3 acquire_ec2.py --case "CASE-NAME-AND-ID" --instance-list ./ec2_list.txt --s3-bucket forensic-foo
```

In this example, the instance IDs that should be acquired are placed in the file `ec2_list.txt`.
The volume images will be written to the S3 bucket `forensic-foo` under the case `CASE-NAME-AND-ID`.

To use a customer managed KMS key, the script can be called as follows.

```
$ sudo python3 acquire_ec2.py --case "CDC-2021-02-26" --instance-list ./ec2_list.txt --s3-bucket forensic-fu --kms-key eebbb888-eeee-4444-baba-085bbbbbbbbb
```

In this example, the KMS key with the ID `eebbb888-eeee-4444-baba-085bbbbbbbbb` is used for encrypting the temporary volumes created during the acquisition process.

## Acquisition Performance
The following performance tests were done in the CDC AWS sandbox environment and compares the acquisition performance of different EC2 instance types used for the acquisition host.
All tests are based on `gp3` EBS volumes with 500 MB/s throughput and at least 4,000 IOPS.

| Instance Type | AMI          | Network | Write to S3 (MB/s) | Read from S3 (MB/s) |
| ------------- | ------------ | ------- | ------------------ | ------------------- |
| `r5n.xlarge`  | Amazon Linux | 25 GBit | 105 MB/s           | 162 MB/s            |
| `r6g.xlarge`  | Ubuntu       | 10 Gbit | 165 MB/s           | 170 MB/s            |
| `r5b.xlarge`  | Amazon Linux | 10 GBit | 150 MB/s           | 133 MB/s            |
