import click
import boto3
import botocore
import asyncio
import json
import time

from . import metadata
from . import config

def camel_dict_to_snake_dict(camel_dict):
    """Convert Boto3 CamelCase dict to snake_case dict"""
    def camel_to_snake(name):

        import re

        first_cap_re = re.compile('(.)([A-Z][a-z]+)')
        all_cap_re = re.compile('([a-z0-9])([A-Z])')
        s1 = first_cap_re.sub(r'\1_\2', name)

        return all_cap_re.sub(r'\1_\2', s1).lower()


    def value_is_list(camel_list):

        checked_list = []
        for item in camel_list:
            if isinstance(item, dict):
                checked_list.append(camel_dict_to_snake_dict(item))
            elif isinstance(item, list):
                checked_list.append(value_is_list(item))
            else:
                checked_list.append(item)

        return checked_list


    snake_dict = {}
    for k, v in camel_dict.items():
        if isinstance(v, dict):
            snake_dict[camel_to_snake(k)] = camel_dict_to_snake_dict(v)
        elif isinstance(v, list):
            snake_dict[camel_to_snake(k)] = value_is_list(v)
        else:
            snake_dict[camel_to_snake(k)] = v

    return snake_dict


def tag_list_to_dict(tags_list):
    """Convert Boto3-style key-value tags list into dict"""
    tags_dict = {}

    for tag in tags_list:
        if 'key' in tag:
            tags_dict[tag['key']] = tag['value']
        elif 'Key' in tag:
            tags_dict[tag['Key']] = tag['Value']

    return tags_dict


def get_instance_region():
    data = {}
    fetcher = botocore.InstanceMetadataFetcher()

    try:
        r = fetcher._get_request('http://169.254.169.254/latest/dynamic/instance-identity/document', fetcher._timeout, fetcher._num_attempts)
        if r.content:
            val = r.content.decode('utf-8')
            if val[0] == '{':
                data = json.loads(val)
    except botocore._RetriesExceededError:
        print("Max number of attempts exceeded ({0}) when attempting to retrieve data from metadata service.".format(num_attempts))

    return data.get('region', None)


def create_session(config):
    session_config = dict(profile_name=config.profile_name, region_name=config.region_name)
    session = boto3.Session(**session_config)

    # IAM Role credentials don't provide a default region (unlike Lambda and Profiles)
    if session.get_credentials().method == 'iam-role' and not session.region_name:
        session_config['region_name'] = get_instance_region()
        session = boto3.Session(**session_config)

    if config.role_arn:
        sts_client = session.client('sts')
        role_session_name = 'squid.aws-acl-helper.session-{0}'.format(time.time())

        assumed_role = sts_client.assume_role(RoleArn=config.role_arn, ExternalId=config.external_id, RoleSessionName=role_session_name)
        session_config['aws_access_key_id'] = assumed_role['Credentials']['AccessKeyId']
        session_config['aws_secret_access_key'] = assumed_role['Credentials']['SecretAccessKey']
        session_config['aws_session_token'] = assumed_role['Credentials']['SessionToken']
        session = boto3.Session(**session_config)

    return session


def store_aws_metadata(config):
    """Store AWS metadata (result of ec2.describe_instances call) into Redis"""
    loop = asyncio.get_event_loop()
    session = create_session(config)
    ec2_client = session.client('ec2')
    response = ec2_client.describe_instances()
    tasks = []

    # Find all instances, convert to snake dict, convert to tags, and fire off
    # task to store in Redis
    for reservation in response.get('Reservations', []):
        for instance in reservation.get('Instances', []):
            instance = camel_dict_to_snake_dict(instance)
            instance['tags'] = tag_list_to_dict(instance.get('tags', []))
            print('Storing data for {instance_id}'.format(**instance))
            tasks.append(loop.create_task(metadata.store(config,instance)))

    if len(tasks) > 0: 
        loop.run_until_complete(asyncio.wait(tasks))
        loop.stop()

@click.option(
    '--ttl', 
    default=1800,
    type=int, 
    help='Time-to-live for AWS metadata stored in Redis.')
@click.option(
    '--port',
    default=6379,
    type=int,
    help='Redis server port.'
)
@click.option(
    '--host',
    default='localhost',
    type=str,
    help='Redis server hostname.'
)
@click.option(
    '--region',
    default=None,
    type=str,
    help='AWS Region name (overrides region from profile).'
)
@click.option(
    '--profile',
    default=None,
    type=str,
    help='AWS Configuration Profile name.'
)
@click.option(
    '--role-arn',
    default=None,
    type=str,
    help='The Amazon Resource Name (ARN) of the role to assume.'
)
@click.option(
    '--external-id',
    default=None,
    type=str,
    help='A unique identifier that is used by third parties when assuming roles in their customers\' accounts.'
)
@click.command()
def sync(**args):
    """Collect inventory from EC2 and persist to Redis"""
    _config = config.Config(**args)
    store_aws_metadata(_config)

