"""Decode one dumped OTLP/protobuf trace body: spans per request, span names, and the size of each attribute.
Usage: python decode.py <body.bin>   (needs opentelemetry-proto, present in the tracing venvs)"""
import sys, collections
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

req = ExportTraceServiceRequest(); req.ParseFromString(open(sys.argv[1], 'rb').read())
spans = [s for rs in req.resource_spans for ss in rs.scope_spans for s in ss.spans]
names = collections.Counter(s.name for s in spans)
print('spans', len(spans), dict(names))
s = spans[0]
print('first span:', s.name, 'attrs', len(s.attributes), 'span bytes', s.ByteSize())
for a in sorted(s.attributes, key=lambda a: -a.ByteSize())[:12]:
    print(f'  {a.ByteSize():6d}  {a.key}')
