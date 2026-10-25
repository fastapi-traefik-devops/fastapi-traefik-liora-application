FROM jenkins/jenkins:2.587-alpine-jdk21@sha256:15209e2ad658bdf711f9db0d1e70b58313e24df8dbdf34ed36b0d404b41fc1d1
USER root
RUN apk upgrade --no-cache && mkdir -p /opt/phase1-bootstrap && touch /opt/phase1-bootstrap/Jenkinsfile /opt/phase1-bootstrap/Jenkinsfile.publish
USER jenkins
COPY --chown=jenkins:jenkins plugins.lock.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --latest false --plugin-file /usr/share/jenkins/ref/plugins.txt
USER root
# Plugin installation is a build operation; its bundled libraries are not
# needed by the running controller.
RUN rm -f /opt/jenkins-plugin-manager.jar /bin/jenkins-plugin-cli /usr/bin/jenkins-plugin-cli
USER jenkins
ENV CASC_JENKINS_CONFIG=/bootstrap/jenkins.yaml
ENV JAVA_OPTS="-Djenkins.install.runSetupWizard=false"
