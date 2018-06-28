#!/usr/bin/env python3
import sys, os
import boto3, botocore
import datetime

from pprint import pprint
from cleanup import cleanup_version_stack
from deploy import get_list_of_rules

cloudformation = boto3.client('cloudformation')

def get_alb_default_target_group(cluster_name, app_name):

    alb_stack_name = 'ECS-{cluster_name}-App-{app_name}'.format(
        cluster_name=cluster_name,
        app_name=app_name
    )

    alb_default_target_group = ""

    rules = get_list_of_rules()
    for rule in rules:
        if rule['IsDefault'] == True:
            alb_default_target_group = rule['Actions'][0]['TargetGroupArn']
            break

    if alb_default_target_group == "":
        raise Exception("Default action target group not found in ALB Listener")

    return alb_default_target_group

def list_stacks(cluster_name, app_name):

    stack_name_prefix = "ECS-{cluster_name}-App-{app_name}-".format(
        cluster_name=cluster_name,
        app_name=app_name
    )
    stack_description = "ECS Cluster Application Version"

    stack_list = []

    paginator = cloudformation.get_paginator('list_stacks')
    response_iterator = paginator.paginate(StackStatusFilter=['CREATE_COMPLETE', 'UPDATE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE'])
    for page in response_iterator:
        stacks = page['StackSummaries']
        for stack in stacks:
            if not stack['StackName'].startswith(stack_name_prefix):
                continue
            if "".join(stack['TemplateDescription']) != stack_description:
                continue

            # Check if application name is correct:
            response = cloudformation.describe_stacks(
                StackName=stack['StackName']
            )
            stack_details = response['Stacks'][0]
            stack_param_name = "undefined"

            for stack_param in stack_details['Parameters']:
                if stack_param['ParameterKey'] == 'Name':
                    stack_param_name = stack_param['ParameterValue']

            if stack_param_name != app_name:
                continue

            stack_list.append(stack)

    return stack_list

def filter_old_stacks(stacks, age_seconds):
    filtered_stacks = []

    for stack in stacks:
        if stack['CreationTime'].timestamp() < datetime.datetime.now().timestamp() - int(age_seconds):
            filtered_stacks.append(stack)

    return filtered_stacks

def filter_not_cutover(stacks, cluster_name, app_name):
    filtered_stacks = []

    alb_default_target_group = get_alb_default_target_group(cluster_name, app_name)

    for stack in stacks:
        # Fetching Application Version TargetGroup ARN
        response = cloudformation.describe_stack_resources(
            StackName=stack['StackName'],
            LogicalResourceId='ALBTargetGroup'
        )
        target_group = response['StackResources'][0]['PhysicalResourceId']
        if target_group != alb_default_target_group:
            filtered_stacks.append(stack)

    return filtered_stacks

def filter_excludes(stacks, stack_excludes):
    filtered_stacks = []

    stack_excludes_list = [item.strip() for item in stack_excludes.split(',')]

    for stack in stacks:
        exclude = False

        for stack_excludes in stack_excludes_list:
            if stack['StackName'].startswith(stack_excludes):
                exclude = True

        if not exclude:
            filtered_stacks.append(stack)

    return filtered_stacks

def get_stack_version(stack_name):
    response = cloudformation.describe_stacks(StackName=stack_name)
    for output in response['Stacks'][0]['Outputs']:
        if output['OutputKey'] == 'Version':
            return output['OutputValue']

def get_stack_termination_protection(stack_name):
    response = cloudformation.describe_stacks(StackName=stack_name)
    return response['Stacks'][0]['EnableTerminationProtection']

if __name__ == "__main__":
    stacks = list_stacks(cluster_name=os.environ['ECS_CLUSTER_NAME'], app_name=os.environ['ECS_APP_NAME'])

    # Filter live stacks from the rest
    stacks = filter_not_cutover(stacks=stacks, cluster_name=os.environ['ECS_CLUSTER_NAME'], app_name=os.environ['ECS_APP_NAME'])

    # Filter stacks excluded from cleanup
    if 'ECS_AUTOCLEANUP_EXCLUDES' in os.environ:
        stacks = filter_excludes(stacks, os.environ['ECS_AUTOCLEANUP_EXCLUDES'])

    # Filter stacks older than N number of seconds
    if 'ECS_AUTOCLEANUP_OLDER_THAN' in os.environ:
        stacks = filter_old_stacks(stacks, os.environ['ECS_AUTOCLEANUP_OLDER_THAN'])

    for stack in stacks:
        version = get_stack_version(stack['StackName'])

        stack_name = "{cluster_name}-{app_name}-{version}".format(
            cluster_name=os.environ['ECS_CLUSTER_NAME'],
            app_name=os.environ['ECS_APP_NAME'],
            version=version)

        if get_stack_termination_protection(stack['StackName']):
            print("Skipping {stack_name}, Termination Protection Enabled".format(stack_name=stack_name))
            continue

        if 'ECS_AUTOCLEANUP_DRY_RUN' in os.environ and os.environ['ECS_AUTOCLEANUP_DRY_RUN'] == 'true':
            print("Skipping {stack_name}, Dry-run Enabled".format(stack_name=stack_name))
            continue

        print("Cleaning up {stack_name}".format(stack_name=stack_name))

        cleanup_version_stack(cluster_name=os.environ['ECS_CLUSTER_NAME'], app_name=os.environ['ECS_APP_NAME'], version=version)
