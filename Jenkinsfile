import org.jenkinsci.plugins.pipeline.modeldefinition.Utils

library('JenkinsPipelineUtils') _

podTemplate(inheritFrom: 'jenkins-agent kaniko') {
    node(POD_LABEL) {
        def variants
        def descendants
        def built = false

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
                helmCharts.kaniko([
                    "registry:5000/iac:${currentBuild.number}",
                    "registry:5000/iac:latest"
                ])
            }
        }
    }
}
