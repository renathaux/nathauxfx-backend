FROM ghcr.io/renathaux/nathauxfx-backend@sha256:4f11f091d8ba77192a81569b3e85e101dcccc329c641eddeabd160b13be6fb49
USER 0:0
COPY --chmod=0555 maintenance/ /usr/local/lib/flowsignal-maintenance/
# Render supplies SSH access; no SSH server or keys are installed. NP is an
# invalid password hash, not a password, and does not lock public-key login.
RUN mkdir -p /root/.ssh && chmod 0700 /root/.ssh && \
    /usr/sbin/usermod --password NP root
ENTRYPOINT ["/opt/venv/bin/python", "-I", "-B", "/usr/local/lib/flowsignal-maintenance/launcher.py"]
CMD []
