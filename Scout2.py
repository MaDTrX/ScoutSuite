#!/usr/bin/env python

# Import the Amazon SDK
import boto
import boto.ec2

# Other imports
import argparse
import json
import os
import re
import urllib
import urllib2
import uuid

# Set two environment variables as required by Boto
#os.environ["AWS_ACCESS_KEY_ID"] = 'XXXXX'
#os.environ["AWS_SECRET_ACCESS_KEY"] = 'XXXXX'


########################################
##### Misc functions
########################################

def fetch_creds_from_instance_metadata():
    base_url = 'http://169.254.169.254/latest/meta-data/iam/security-credentials'
    try:
        iam_role = urllib2.urlopen(base_url).read()
        credentials = json.loads(urllib2.urlopen(base_url + '/' + iam_role).read())
        return credentials['AccessKeyId'], credentials['SecretAccessKey']
    except Exception, e:
        print 'Failed to fetch credentials. Make sure that this EC2 instance has an IAM role (%s)' % e
        return None, None

def fetch_creds_from_csv(filename):
    key_id = None
    secret = None
    with open(filename, 'rt') as csvfile:
        for i, line in enumerate(csvfile):
            if i == 1:
                username, key_id, secret = line.split(',')
    return key_id, secret

def manage_dictionary(dictionary, key, init, callback=None):
    if not str(key) in dictionary:
        dictionary[str(key)] = init
        manage_dictionary(dictionary, key, init)
        if callback:
            callback(dictionary[key])
    return dictionary

def save_to_file(blob, keyword, force_write):
    print 'Saving ' + keyword + ' data...'
    filename = 'aws_' + keyword.lower().replace(' ','_') + '.json'
    if not os.path.isfile(filename) or force_write:
        with open(filename, 'wt') as f:
            print 'Success: saved data to ' + filename
            print >>f, json.dumps(blob, indent=4, separators=(',', ': '), sort_keys=True)
    else:
        print 'Error: ' + filename + ' already exists.'


########################################
##### EC2 functions
########################################

def get_security_groups_info(ec2, region):
    groups = ec2.get_all_security_groups()
    security_groups = []
    for group in groups:
        security_group = {}
        security_group['name'] = group.name
        security_group['id'] = group.id
        security_group['description'] = group.description
        security_group = manage_dictionary(security_group, 'running-instances', [])
        security_group = manage_dictionary(security_group, 'stopped-instances', [])
        protocols = {}
        for rule in group.rules:
            protocols = manage_dictionary(protocols, rule.ip_protocol, {})
            protocols[rule.ip_protocol] = manage_dictionary(protocols[rule.ip_protocol], 'rules', [])
            protocols[rule.ip_protocol]['name'] = rule.ip_protocol.upper()
            acl = {}
            acl['grants'] = []
            # Save grants, values are either a CIDR or an EC2 security group
            for grant in rule.grants:
                if grant.cidr_ip:
                    acl['grants'].append(grant.cidr_ip)
                else:
                    acl['grants'].append('%s (%s)' % (grant.name, grant.groupId))
            # Save the port (single port or range)
            if rule.from_port == rule.to_port:
                acl['ports'] = rule.from_port
            else:
                acl['ports'] = '%s-%s' % (rule.from_port, rule.to_port)
            # Save the new rule
            protocols[rule.ip_protocol]['rules'].append(acl)
        # Save all the rules, sorted by protocol
        security_group['protocols'] = protocols
        # Save all instances associated with this group
        for i in group.instances():
            if i.state == 'running':
                security_group['running-instances'].append(i.id)
            else:
                security_group['stopped-instances'].append(i.id)
        # Append the new security group to the return list
        security_groups.append(security_group)
    return security_groups

def get_instances_info(ec2, region):
    results = []
    reservations = ec2.get_all_reservations()
    for reservation in reservations:
        groups = []
        for g in reservation.groups:
            groups.append(g.name)
        for i in reservation.instances:
            instance = {}
            instance['reservation_id'] = reservation.id
            instance['groups'] = groups
            instance['region'] = region
            # Get instance variables (see http://boto.readthedocs.org/en/latest/ref/ec2.html#module-boto.ec2.instance to see what else is available)
            for key in ['id', 'public_dns_name', 'private_dns_name', 'key_name', 'launch_time', 'private_ip_address', 'ip_address']:
                instance[key] = i.__dict__[key]
            # FIXME ... see why it's not working when added in the list above
            instance['state'] = i.state
            results.append(instance)
    return results


########################################
##### IAM functions
########################################

def get_groups_info(iam, permissions):
    groups = iam.get_all_groups()
    for group in groups.list_groups_response.list_groups_result.groups:
        group['users'] = get_group_users(iam, group.group_name);
        group['policies'], permissions = get_policies(iam, permissions, 'group', group.group_name)
    return groups, permissions

def get_group_users(iam, group_name):
    users = []
    fetched_users = iam.get_group(group_name).get_group_response.get_group_result.users
    for user in fetched_users:
        users.append(user.user_name)
    return users

def get_permissions(policy_document, permissions, keyword, name, policy_name):
    document = json.loads(urllib.unquote(policy_document).decode('utf-8'))
    for statement in document['Statement']:
        if 'Effect' and 'Action' in statement:
            effect = str(statement['Effect'])
            for action in statement['Action']:
                permissions = manage_dictionary(permissions, action, {})
                permissions[action] = manage_dictionary(permissions[action], effect, {})
                permissions[action][effect] = manage_dictionary(permissions[action][effect], keyword, [])
                entry = {}
                entry['name'] = name
                entry['policy_name'] = policy_name
                permissions[action][effect][keyword].append(entry)
    return permissions

def get_policies(iam, permissions, keyword, name):
    fetched_policies = []
    if keyword == 'role':
        m1 = getattr(iam, 'list_role_policies', None)
    else:
        m1 = getattr(iam, 'get_all_' + keyword + '_policies', None)
    if m1:
        policy_names = m1(name)
    else:
        return fetched_policies, permissions
    policy_names = policy_names['list_' + keyword + '_policies_response']['list_' + keyword + '_policies_result']['policy_names']
    get_policy_method = getattr(iam, 'get_' + keyword + '_policy')
    for policy_name in policy_names:
        r = get_policy_method(name, policy_name)
        r = r['get_'+keyword+'_policy_response']['get_'+keyword+'_policy_result']
        pdetails = {}
        pdetails['policy_name'] = policy_name
        pdetails['policy_document'] = r.policy_document
        fetched_policies.append(pdetails)
        permissions = get_permissions(pdetails['policy_document'], permissions, keyword + 's', name, pdetails['policy_name'])
    return fetched_policies, permissions


def get_roles_info(iam, permissions):
    roles = iam.list_roles()
    for role in roles.list_roles_response.list_roles_result.roles:
        role['policies'], permissions = get_policies(iam, permissions, 'role', role.role_name)
    return roles, permissions

def get_users_info(iam, permissions):
    users = iam.get_all_users()
    for user in users.list_users_response.list_users_result.users:
        user['policies'], permissions = get_policies(iam, permissions, 'user', user.user_name)
        groups = iam.get_groups_for_user(user['user_name'])
        user['groups'] = groups.list_groups_for_user_response.list_groups_for_user_result.groups
        try:
            logins = iam.get_login_profiles(user['user_name'])
            user['logins'] = logins.get_login_profile_response.get_login_profile_result.login_profile
        except Exception, e:
            pass
        access_keys = iam.get_all_access_keys(user['user_name'])
        user['access_keys'] = access_keys.list_access_keys_response.list_access_keys_result.access_key_metadata
        mfa_devices = iam.get_all_mfa_devices(user['user_name'])
        user['mfa_devices'] = mfa_devices.list_mfa_devices_response.list_mfa_devices_result.mfa_devices

    return users, permissions


########################################
##### S3 functions
########################################

def init_s3_permissions(grant):
    grant['read'] = False
    grant['write'] = False
    grant['acp_read'] = False
    grant['acp_write'] = False
    return grant

def set_s3_permission(grant, name):
    if name == 'READ' or name == 'FULL_CONTROL':
        grant['read'] = True
    if name == 'WRITE' or name == 'FULL_CONTROL':
        grant['write'] = True
    if name == 'ACP_READ' or name == 'FULL_CONTROL':
        grant['acp_read'] = True
    if name == 'ACP_WRITE' or name == 'FULL_CONTROL':
        grant['acp_write'] = True

def s3_group_to_string(uri):
    if uri == 'http://acs.amazonaws.com/groups/global/AuthenticatedUsers':
        return 'Authenticated users'
    elif uri == 'http://acs.amazonaws.com/groups/global/AllUsers':
        return 'All users'
    elif uri == 'http://acs.amazonaws.com/groups/s3/LogDelivery':
        return 'Log delivery'
    else:
        return uri

def get_s3_bucket_versioning(bucket):
    r = bucket.get_versioning_status()
    if 'Versioning' in r:
        return r['Versioning']
    else:
        return 'Disabled'

def get_s3_bucket_logging(bucket):
    r = bucket.get_logging_status()
    if r.target is not None:
        return r.target + '/' + r.prefix
    else:
        return 'Disabled'

# List all available buckets
def get_s3_buckets(s3):
    s3_buckets = []
    buckets = s3.get_all_buckets()
    for b in buckets:
        bucket = {}
        bucket['name'] = b.name
        acp = b.get_acl()
        bucket['grants'] = {}
        for grant in acp.acl.grants:
            grantee_name = 'Unknown'
            if grant.type == 'Group':
                grantee_name = s3_group_to_string(grant.uri)
                grant.uri.rsplit('/',1)[0]
            elif grant.type == 'CanonicalUser':
                grantee_name = grant.display_name
            manage_dictionary(bucket['grants'], grantee_name, {}, init_s3_permissions)
            set_s3_permission(bucket['grants'][grantee_name], grant.permission)
            bucket['grants'][grantee_name]['email'] = grant.email_address
        bucket['creation_date'] = b.creation_date
        bucket['region'] = b.get_location()
        bucket['logging'] = get_s3_bucket_logging(b)
        bucket['versioning'] = get_s3_bucket_versioning(b)
        s3_buckets.append(bucket)
    return s3_buckets


########################################
##### Main
########################################

def main(args):

    key_id = None
    secret = None

    # Fetch credentials from the EC2 instance's metadata
    if args.fetch_creds_from_instance_metadata:
        key_id, secret = fetch_iam_role_credentials()

    # Fetch credentials from CSV
    if args.fetch_creds_from_csv is not None:
        key_id, secret = fetch_creds_from_csv(args.fetch_creds_from_csv[0])

    # Fetch credentials from environment
    if 'AWS_ACCESS_KEY_ID' in os.environ and 'AWS_SECRET_ACCESS_KEY' in os.environ:
        key_id = os.environ["AWS_ACCESS_KEY_ID"]
        secret = os.environ["AWS_SECRET_ACCESS_KEY"] = secret

    if key_id is None or secret is None:
        print 'Error: you need to set your AWS credentials as environment variables to use Scout2.'
        return -1

    ##### IAM
    if args.fetch_iam:
        try:
            iam = boto.connect_iam(key_id, secret)
            permissions = {}
            print 'Fetching IAM users data...'
            users, permissions = get_users_info(iam, permissions)
            save_to_file(users, 'IAM users', args.force_write)
            print 'Fetching IAM groups data...'
            groups, permissions = get_groups_info(iam, permissions)
            save_to_file(groups, 'IAM groups', args.force_write)
            print 'Fetching IAM roles data...'
            roles, permissions = get_roles_info(iam, permissions)
            save_to_file(roles, 'IAM roles', args.force_write)
            p = {}
            p['permissions'] = permissions
            save_to_file(p, 'IAM permissions', args.force_write)
        except Exception, e:
            print 'Exception:\n %s' % e
            pass

    ##### EC2
    if args.fetch_ec2:
      security_groups = {}
      security_groups['security_groups'] = []
      instances = {}
      instances['instances'] = []
      ec2_connection = boto.connect_ec2(key_id, secret)
      for region in boto.ec2.regions():
          if region.name != 'us-gov-west-1' or args.fetch_ec2_gov:
            try:
                print 'Fetching EC2 data for region %s' % region.name
                security_groups['security_groups'] += get_security_groups_info(ec2_connection, region.name)
                instances['instances'] += get_instances_info(ec2_connection, region.name)
            except Exception, e:
                print 'Exception: \n %s' % e
                pass
      save_to_file(security_groups, 'EC2 security groups', args.force_write)
      save_to_file(instances, 'EC2 instances', args.force_write)

    ##### S3
    if args.fetch_s3:
        s3_buckets = {}
        s3_buckets['buckets'] = []
        s3_connection = boto.connect_s3(key_id, secret)
        print 'Fetching S3 buckets data...'
        s3_buckets['buckets'] = get_s3_buckets(s3_connection)
        save_to_file(s3_buckets, 'S3 buckets', args.force_write)


########################################
##### Argument parser
########################################
parser = argparse.ArgumentParser()
parser.add_argument('--no_iam',
                    dest='fetch_iam',
                    default=True,
                    action='store_false',
                    help='don\'t fetch the IAM configuration')
parser.add_argument('--no_ec2',
                    dest='fetch_ec2',
                    default=True,
                    action='store_false',
                    help='don\'t fetch the EC2 configuration')
parser.add_argument('--no_s3',
                    dest='fetch_s3',
                    default='True',
                    action='store_false',
                    help='don\'t fetch the S3 configuration')
parser.add_argument('--gov',
                    dest='fetch_ec2_gov',
                    default=False,
                    action='store_true',
                    help='fetch the EC2 configuration from the us-gov-west-1 region')
parser.add_argument('--force',
                    dest='force_write',
                    default=False,
                    action='store_true',
                    help='overwrite existing json files')
parser.add_argument('--role-credentials',
                    dest='fetch_creds_from_instance_metadata',
                    default=False,
                    action='store_true',
                    help='fetch credentials for this EC2 instance')
parser.add_argument('--credentials',
                    dest='fetch_creds_from_csv',
                    default=None,
                    nargs='+',
                    help='credentials file')

args = parser.parse_args()

if __name__ == '__main__':
    main(args)