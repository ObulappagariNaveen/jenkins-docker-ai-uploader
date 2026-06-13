pipeline {
    agent {
        label 'jenkins-infra-prd-jenkins-agent'
    }

    stages {
        stage('Find source Jenkins job') {
            steps {
                script {
                    def causes = currentBuild.getBuildCauses()
                    def upstreamCause = causes.find { cause ->
                        cause._class?.contains('UpstreamCause')
                    }

                    if (upstreamCause == null) {
                        env.SOURCE_JOB_NAME = ''
                        echo 'No upstream Jenkins job found. This AI job should be triggered by Build other projects.'
                    } else {
                        def upstreamProject = upstreamCause.upstreamProject ?: ''
                        env.SOURCE_JOB_NAME = upstreamProject.tokenize('/').last()
                        echo "Triggered by Jenkins job: ${env.SOURCE_JOB_NAME}"
                    }
                }
            }
        }

        stage('Checkout') {
            when {
                expression { return env.SOURCE_JOB_NAME?.trim() }
            }
            steps {
                container('devops-tools') {
                    git branch: 'main',
                        url: 'https://github.com/ObulappagariNaveen/jenkins-docker-ai-uploader.git'
                }
            }
        }

        stage('Run watcher once') {
            when {
                expression { return env.SOURCE_JOB_NAME?.trim() }
            }
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

                        python3 watch_jenkins_failures.py --once --summary --jobs "$SOURCE_JOB_NAME"
                        '''
                    }
                }
            }
        }

        stage('Archive output') {
            when {
                expression { return env.SOURCE_JOB_NAME?.trim() }
            }
            steps {
                archiveArtifacts artifacts: 'jenkins-ai-data/output/*.txt,jenkins-ai-data/input/*.txt,jenkins-ai-data/support/*.csv', allowEmptyArchive: true
            }
        }
    }
}
