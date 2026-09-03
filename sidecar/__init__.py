"""Agent-Colab Secret sidecar (development plan §9.4; P4-12).

Runs on the Agent host, resolves one-time secret handles against the Broker and injects the
values into a local process through a Unix domain socket, the process environment or a file
descriptor. Values never touch disk or logs; revocations are applied within 5 seconds.
"""
