library('JenkinsPipelineUtils') _

podTemplate(inheritFrom: 'jenkins-agent kaniko') {
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
            if (fileExists('docs/architecture/ansible-architecture.yaml')) {
                sh './scripts/arch-validate docs/architecture/ansible-architecture.yaml'
            } else {
                echo 'docs/architecture/ansible-architecture.yaml not present yet — skipping.'
            }
        }

        stage('Archive architecture artifact') {
            archiveArtifacts artifacts: 'docs/architecture/*.yaml', fingerprint: true
        }
    }
}
