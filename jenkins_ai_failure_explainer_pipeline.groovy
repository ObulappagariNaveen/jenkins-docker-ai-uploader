pipeline {
    agent {
        label 'jenkins-infra-prd-jenkins-agent'
    }

    triggers {
        cron('H/1 * * * *')
    }

    stages {
        stage('Checkout') {
            steps {
                container('devops-tools') {
                    git branch: 'main',
                        url: 'https://github.com/ObulappagariNaveen/jenkins-docker-ai-uploader.git'
                }
            }
        }

        stage('Run watcher once') {
            steps {
                container('devops-tools') {
                    withCredentials([
                        string(credentialsId: 'jenkins-api-token-for-ai-watcher', variable: 'JENKINS_API_TOKEN')
                    ]) {
                        sh '''
                        set +x
                        export JENKINS_USER="obulappagarinaveen"
                        export JENKINS_BASE_URL="https://jenkins.prd.valmo.in/job/support/job/log10/job/Regular_tasks"
                        export DATA_DIR="$WORKSPACE/jenkins-ai-data"

                        python3 watch_jenkins_failures.py --once --summary
                        '''
                    }
                }
            }
        }

        stage('Archive output') {
            steps {
                archiveArtifacts artifacts: 'jenkins-ai-data/output/*.txt,jenkins-ai-data/input/*.txt,jenkins-ai-data/support/*.csv', allowEmptyArchive: true
            }
        }
    }
}
