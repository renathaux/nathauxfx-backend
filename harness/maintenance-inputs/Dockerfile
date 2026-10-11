FROM certified-production
USER 0:0
COPY --chmod=0555 maintenance/ /usr/local/lib/flowsignal-maintenance/
# Render supplies SSH access; no SSH server or keys are installed. NP is an
# invalid password hash, not a password, and does not lock public-key login.
RUN mkdir -p /root/.ssh && chmod 0700 /root/.ssh && \
    /usr/sbin/usermod --password NP root
ENTRYPOINT ["/opt/venv/bin/python", "-I", "-B", "/usr/local/lib/flowsignal-maintenance/launcher.py"]
CMD []
