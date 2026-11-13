# The test image: the service image plus the egress audit hook. Built only by the test
# harness, from the egress_hook folder; the product image has no hook.
ARG BASE
FROM ${BASE}
COPY 00_egress_audit.pth egress_audit.py /tmp/hook/
RUN site=$(python -c "import sysconfig; print(sysconfig.get_path('purelib'))") \
 && cp /tmp/hook/00_egress_audit.pth /tmp/hook/egress_audit.py "$site"/ \
 && rm -rf /tmp/hook
