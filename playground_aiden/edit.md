1. adaptor가 이상함 - Adaptor 삭제하기
    - 현재 prometheus나 elastic search는 띄우지말기
2. observer가 원하는 형식이 있는거 같음. 지금은 observer없이 구현하기
    - aiopslab/observer/observe.py
    - observe 새로 파고, traceAPI, logAPI, metricAPI를 docker config맞게 수정하기
    - 데이터셋마다 다르게 하기
3. kuberctl로 접근하나 docker명령어로 접근하나 같은거ㅏ?
4. deploy관련 docker파일은 aiopslab-applications 디렉토리에 만들기
    - docker 동작에 관한 정의인 Python 코드들도 같은 디렉토리 안에 넣기
5. 상위레벨에서 namepsace만들고 지우는걸 aiopslab/service/apps/* 에 넣기
    1. App을 관리하는 역할을 여기에 넣기.  Config파일 불러오기
6. 타임 매핑이랑 등등은 app 폴더 안에 넣기
    - Adapter는 없어도 될듯
7. static_problems/ 로 directory 분리하기 거기에 registry.py를 추가
    - 데이터셋마다 problem 정의와 형식이 다름. 이를 고려해서 코드를 작성해야함
    - 여기에 history telemetry 데이터셋 미리 넣어놓는것까지 구현하기
    - .csv파일에 있는 Timestamp를 docker 기준으로 바꾸기
    - static_problems에서는 데이터셋마다 폴더를 나눠서 problem정의하기
8. [Orchestrator.py](http://Orchestrator.py) 관련해서 [base.py](http://base.py) 만들어서 공통적인 메소드 넣어놓고
    - static_orchestrator 만들어서 거기에 static 한정 메소드 추가하기
9. Actions 분리 → static_actions 디렉토리

수정한 이유가 명확해야함?