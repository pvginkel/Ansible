library('JenkinsPipelineUtils') _

podTemplate(inheritFrom: 'jenkins-agent kaniko', containers: [
    containerTemplates.python('python')
]) {
    node(POD_LABEL) {
        stage('Cloning repo') {
            checkout scm
        }

        stage('Building iac image') {
            copyArtifacts(
                projectName: 'HomelabTerraformProvider',
                filter: 'terraform-provider-homelab*',
                target: 'artifacts'
            )

            container('kaniko') {
                helmCharts.kaniko('support/iac-image/Dockerfile', '.', [
                    "registry:5000/iac:${currentBuild.number}",
                    "registry:5000/iac:latest"
                ])
            }
        }

        stage('Validate architecture artifact') {
            container('python') {
                sh './scripts/arch-validate.py docs/architecture/ansible-architecture.yaml'
            }
        }

        stage('Archive architecture artifact') {
            archiveArtifacts artifacts: 'docs/architecture/*.yaml', fingerprint: true
        }
    }
}
