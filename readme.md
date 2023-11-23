# Extended AWS instance scheduler for eks pod
## 개요
aws 에서 제공하는 스케쥴러는 ec2, rds 에 대해서만 스케쥴링을 제공한다(2023/11/23 기준).  
eks pod 의 경우 켜져 있는 동안 비용이 나오기 때문에 dev 환경의 경우 on/off 스케쥴링을 통해 비용절감을 할 수 있다.  
해당 코드는 aws instance scheduler 1.5.0 version 에 기반하였다.  

## 특이사항
pod의 제어는 deployment 와 rollout의 replica 값을 바꿔주는 것을 통해 수행한다.  
kubernetes 자원 제어를 위해 kubernetes client library 를 사용하였다.  
https://github.com/kubernetes-client/python/  
kubernetes 자원은 tag를 가지고 있지 않기 때문에 annotation 을 tag 처럼 사용하였다.  
eks service 는 replica 값을 가지고 있기 때문에 켜고 끌 때 이 값을 별도로 처리해줘야 한다.  
이를 위해 deployment/rollouts의 annotation에 값을 저장하고 이를 불러와 사용하도록 구성하였다.  

## 업데이트 예정
- replica resize 기능
- kubernetes client library pagination 관련 코드 보완
- aws-auth 상세 설명

## 적용
### 1. lambda 에 권한 추가
aws cloudformation stack template 를 통해 생성되는 lambda function 에 아래와 같은 권한을 추가해줘야 한다  
```json
{
	"Version": "2012-10-17",
	"Statement": [
		{
			"Action": [
				"eks:list*",
				"eks:describe*"
			],
			"Resource": "*",
			"Effect": "Allow",
			"Sid": "EKSCluster"
		}
	]
}
```

### 2. aws-auth 추가
clusterRole 과 clusterRoleBinding을 통해 schedule의 lambda role에 deployment/rollout 에 대한 권한을 부여해 줘야 한다.  
list, patch는 필수로 포함되어야 한다.


### 3. lambda 코드의 configuration 폴더 작업
path : instance_scheduler > configuration > config_admin.py

```python
...

class ConfigAdmin:
    """
    Implements admin api for Scheduler
    """

    TYPE_ATTR = "type"
    # regex for checking time formats H:MM and HH:MM
    TIME_REGEX = "^([0|1]?[0-9]|2[0-3]):[0-5][0-9]$"

    SUPPORTED_SERVICES = ["ec2", "rds", "eks"] # here

...

```


### 4. lambda 코드의 schedulers 폴더 작업
#### 4.1 \_\_init\_\_.py
path : instance_scheduler > scheulders > \_\_init\_\_.py

```python
...

from instance_scheduler.schedulers.ec2_service import Ec2Service
from instance_scheduler.schedulers.rds_service import RdsService
from instance_scheduler.schedulers.eks_service import EksService # here

...

SCHEDULER_TYPES = {"ec2": Ec2Service, "rds": RdsService, "eks": EksService} #here

```

#### 4.2 eks_service.py 파일 추가 및 코드 복사
path : instance_scheduler > scheulders > eks_service.py

### 5. EKS cluster tag 추가
key:value 로 Schedule:on 이 존재해야 해당 cluster에 대해 스케쥴링을 수행한다.  