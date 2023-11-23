import base64
import os
import logging
import re
import boto3

from botocore.signers import RequestSigner
from kubernetes import client, config
from datetime import date, datetime, timezone, timedelta

from instance_scheduler import schedulers
import re
import copy
from botocore.exceptions import ClientError

from instance_scheduler.boto_retry import get_client_with_standard_retry
from instance_scheduler.configuration.instance_schedule import InstanceSchedule
from instance_scheduler.configuration.running_period import RunningPeriod
from instance_scheduler.configuration.scheduler_config_builder import (
    SchedulerConfigBuilder,
)
from instance_scheduler.configuration.setbuilders.weekday_setbuilder import (
    WeekdaySetBuilder,
)

RESTRICTED_EKS_TAG_VALUE_SET_CHARACTERS = r"[^a-zA-Z0-9\s_\.:+/=\\@-]"


INF_FETCHING_RESOURCES = "Fetching eks {} for account {} in region {}"

WARN_TAGGING_STARTED = "Error setting start or stop tags to started instance {}, ({})"
WARN_TAGGING_STOPPED = "Error setting start or stop tags to stopped instance {}, ({})"
WARN_EKS_TAG_VALUE = (
    'Tag value "{}" for tag "{}" changed to "{}" because it did contain characters that are not allowed '
    "in EKS tag values. The value can only contain only the set of Unicode letters, digits, "
    "white-space, '_', '.', '/', '=', '+', '-'"
)


cluster_cache = {}

class EksService:
    EKS_STATE_RUNNING = "running"
    EKS_STATE_STOPPED = "stopped"

    EKS_SCHEDULABLE_STATES = {EKS_STATE_RUNNING, EKS_STATE_STOPPED}

    def __init__(self):
        self.service_name = "eks"
        self.allow_resize = False
        self._instance_tags = None

        self._context = None
        self._session = None
        self._region = None
        self._account = None
        self._logger = None
        self._tagname = None
        self._stack_name = None
        self._config = None

    def _init_scheduler(self, args):
        """
        Initializes common parameters
        :param args: action parameters
        :return:
        """
        self._account = args.get(schedulers.PARAM_ACCOUNT)
        self._context = args.get(schedulers.PARAM_CONTEXT)
        self._logger = args.get(schedulers.PARAM_LOGGER)
        self._region = args.get(schedulers.PARAM_REGION)
        self._stack_name = args.get(schedulers.PARAM_STACK)
        self._session = args.get(schedulers.PARAM_SESSION)
        self._tagname = args.get(schedulers.PARAM_CONFIG).tag_name
        self._config = args.get(schedulers.PARAM_CONFIG)
        self._instance_tags = None

   

    def get_schedulable_resources(self, fn_is_schedulable, fn_describe_name, kwargs):
        self._init_scheduler(kwargs)

        client = get_client_with_standard_retry(
            "eks", session=self._session, region=self._region
        )
        
        resource_name = fn_describe_name.split("_")[-1]
        resource_name = resource_name[0].upper() + resource_name[1:]
        
        args = {}
        resources = [] ## deployment/rollout
        done = False
        self._logger.info(
            INF_FETCHING_RESOURCES, resource_name, self._account, self._region
        )
        cluster_list = client.list_clusters()
        for target_cluster in cluster_list["clusters"] :
            cluster_info = client.describe_cluster(name=target_cluster)
            if "Schedule" not in cluster_info['cluster']['tags'] :
                continue
            if cluster_info['cluster']['tags']['Schedule'] != "on" :
                continue
            
            if target_cluster in cluster_cache:
                cluster_info = cluster_cache[target_cluster]
            else:
                endpoint = cluster_info['cluster']['endpoint']
                cert_authority = cluster_info['cluster']['certificateAuthority']['data']
                cluster_info = {
                    "endpoint" : endpoint,
                    "ca" : cert_authority
                }
                cluster_cache[target_cluster] = cluster_info

            
            #core: pod, custom: rollout, apps: deployment
            core_api, custom_api, apps_api = self.kube_api(target_cluster, cluster_info)
            
            #fetching deployment data
            try:
                ret = apps_api.list_deployment_for_all_namespaces()
                for deploy_data in ret.items:
                    dict_deploy_data=deploy_data.to_dict()
                    if "annotations" not in dict_deploy_data["metadata"]:
                        continue
                    if self._tagname in dict_deploy_data["metadata"]["annotations"] :
                        resource = self._select_deploy_data(clusterName=target_cluster, 
                                                            deployType="deployment", 
                                                            deploy_data=dict_deploy_data, 
                                                            tagname=self._tagname, 
                                                            config=self._config)
                        if resource != {} :
                            resources.append(resource)
            except Exception as ex:
                self._logger.warning("Could not load deployment info {}", str(ex))
                
            #fetching rollout data
            try: 
                ret = custom_api.list_cluster_custom_object(group="argoproj.io", version="v1alpha1", plural="rollouts")
                for deploy_data in ret["items"]:
                    if "annotations" not in deploy_data["metadata"]:
                        continue                    
                    if self._tagname in deploy_data["metadata"]["annotations"] :
                        resource = self._select_deploy_data(clusterName=target_cluster, 
                                                            deployType="rollout", 
                                                            deploy_data=deploy_data, 
                                                            tagname=self._tagname, 
                                                            config=self._config)
                        if resource != {} :
                            resources.append(resource)
            except Exception as ex:
                self._logger.warning("Could not load rollout info {}", str(ex))

        return resources

    def get_schedulable_eks_instances(self, kwargs):
        def is_schedulable_instance(eks_inst):
            return True

        return self.get_schedulable_resources(
            fn_is_schedulable=is_schedulable_instance,
            fn_describe_name="list-deployment-and-rollout",
            kwargs=kwargs,
        )


    def get_schedulable_instances(self, kwargs):
        instances = self.get_schedulable_eks_instances(kwargs)
        return instances


    def _select_deploy_data(self, clusterName, deployType, deploy_data, tagname, config):
        # clusterName
        # deployType : select "deployment" or "rollout"
        # deploy_data : data from deployment or rollout
        
        instanceType = deployType
        
        try:
            replicas = deploy_data["spec"]["replicas"]
            deployName = deploy_data["metadata"]["name"] ## already filtered by "Schedule"
            namespace = deploy_data["metadata"]["namespace"]
            tags = deploy_data["metadata"]["annotations"]
            if "replicas" not in deploy_data["metadata"]["annotations"] and replicas == 0:
                self._logger.warning(
                    "check {}, saved replicas in annotation and current replica", deployName
                )
                return {}
            elif "replicas" not in deploy_data["metadata"]["annotations"] and replicas != 0 :
                self._logger.warning(
                    "In order for {} to be the subject of scheduling, a replicas annotation is required.", deployName
                )
                return {}
            else :
                savedReplicas = deploy_data["metadata"]["annotations"]["replicas"]
            schedule_name = deploy_data["metadata"]["annotations"][tagname] ## get tagname(maybe "Schedule") value
        except Exception as ex:
            self._logger.warrning(
                "{} {} throw exception {}", instanceType, deployName, ex
            )
            return {}
        
        if replicas == 0:
            state = "stopped"
            is_running = False
        else :
            state = "running"
            is_running = True

        return_data = {
            schedulers.INST_HIBERNATE: False,
            schedulers.INST_IS_RUNNING: is_running,
            schedulers.INST_MAINTENANCE_WINDOW: None,
            schedulers.INST_INSTANCE_TYPE: instanceType, #  eks.service.instancetype -> rollout / deployment
            schedulers.INST_CURRENT_STATE: state,
            schedulers.INST_SCHEDULE: schedule_name,
            schedulers.INST_STATE: state,
            schedulers.INST_STATE_NAME: state,
            schedulers.INST_ALLOW_RESIZE: self.allow_resize,
            schedulers.INST_ID: deployName,
            schedulers.INST_ARN: deployName,
            schedulers.INST_TAGS: tags, # saved in annotations
            schedulers.INST_NAME: deployName,
            schedulers.INST_IS_TERMINATED: False,
            "namespace" : namespace,
            "replicas": replicas, ## current pod replicas
            "savedReplicas" : savedReplicas, ## saved replicas in labels
            "clusterName": clusterName,
        }
        
        return return_data


    def resize_instance(self, kwargs):
        pass


    # # noinspection PyMethodMayBeStatic
    def stop_instances(self, kwargs):
        self._init_scheduler(kwargs)
        stopped_instances = kwargs[schedulers.PARAM_STOPPED_INSTANCES]
        for eks_service in stopped_instances:
            try: 
                #core: pod, custom: rollout, apps: deployment
                core_api, custom_api, apps_api = self.kube_api(eks_service.clusterName, cluster_cache[eks_service.clusterName])
                body = {
                    "spec":{
                        "replicas" : 0
                    },
                    "metadata":{
                        "annotations": {
                            "replicas" : str(eks_service.replicas)
                        }
                    }
                }
                if eks_service.instancetype == "rollout":
                    custom_api.patch_namespaced_custom_object(name=eks_service.name, namespace=eks_service.namespace,  body=body, group="argoproj.io", version="v1alpha1", plural="rollouts")
                elif eks_service.instancetype == "deployment":
                    apps_api.patch_namespaced_deployment(name=eks_service.name, namespace=eks_service.namespace, body=body)
                else:
                    self._logger.warning(
                        "{} instanceType should be rollout or deployment", eks_service.id
                    )
                    continue

            except Exception as ex:
                self._logger.error(
                    "stop rollout/deployment failed {}", ex
                )
            yield eks_service.id, "stopped" # schedulers.INST_ID = "id"
        
                
    # # noinspection PyMethodMayBeStatic
    def start_instances(self, kwargs):
        self._init_scheduler(kwargs)
        started_instances = kwargs[schedulers.PARAM_STARTED_INSTANCES]
        for eks_service in started_instances:
            try: 
                #core: pod, custom: rollout, apps: deployment
                core_api, custom_api, apps_api = self.kube_api(eks_service.clusterName, cluster_cache[eks_service.clusterName])
                body = {
                    "spec":{
                        "replicas" : int(eks_service.savedReplicas)
                    }
                }
                if eks_service.instancetype == "rollout":
                    custom_api.patch_namespaced_custom_object(name=eks_service.name, namespace=eks_service.namespace,  body=body, group="argoproj.io", version="v1alpha1", plural="rollouts")
                elif eks_service.instancetype == "deployment":
                    apps_api.patch_namespaced_deployment(name=eks_service.name, namespace=eks_service.namespace, body=body)
                else:
                    self._logger.warning(
                        "{} instanceType should be rollout or deployment", eks_service.id
                    )
                    continue
            except Exception as ex:
                self._logger.error(
                    "start rollout/deployment failed {}", ex
                )
            yield eks_service.id, "running"
        
        
    ## part of kube_api
    def get_bearer_token(self, cluster_name):
        "Create authentication token"
        STS_TOKEN_EXPIRES_IN = 10 # minute
        
        signer = RequestSigner(
            self._session.client('sts').meta.service_model.service_id,
            self._session.region_name,
            'sts',
            'v4',
            self._session.get_credentials(),
            self._session.events
        )
    
        params = {
            'method': 'GET',
            'url': 'https://sts.{}.amazonaws.com/'
                   '?Action=GetCallerIdentity&Version=2011-06-15'.format(self._session.region_name),
            'body': {},
            'headers': {
                'x-k8s-aws-id': cluster_name
            },
            'context': {}
        }
    
        signed_url = signer.generate_presigned_url(
            params,
            region_name=self._session.region_name,
            expires_in=STS_TOKEN_EXPIRES_IN,
            operation_name=''
        )
        base64_url = base64.urlsafe_b64encode(signed_url.encode('utf-8')).decode('utf-8')
    
        # remove any base64 encoding padding:
        return 'k8s-aws-v1.' + re.sub(r'=*', '', base64_url)

    ## kube_api for get eks info
    def kube_api(self, cluster_name, cluster_info):

        cluster = cluster_info
    
        kubeconfig = {
            'apiVersion': 'v1',
            'clusters': [{
              'name': 'cluster1',
              'cluster': {
                'certificate-authority-data': cluster["ca"],
                'server': cluster["endpoint"]}
            }],
            'contexts': [{'name': 'context1', 'context': {'cluster': 'cluster1', "user": "user1"}}],
            'current-context': 'context1',
            'kind': 'Config',
            'preferences': {},
            'users': [{'name': 'user1', "user" : {'token': self.get_bearer_token(cluster_name)}}]
        }
    
        config.load_kube_config_from_dict(config_dict=kubeconfig)
        core_api = client.CoreV1Api() # api_client
        custom_api = client.CustomObjectsApi() # api_client
        apps_api = client.AppsV1Api() # api_client
    
        return core_api, custom_api, apps_api
        