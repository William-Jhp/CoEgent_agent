FROM a2-llm-planner:sim
# Keep the colleague's image unchanged; CoEgent's validator needs JSON Schema.
RUN python3 -m pip install --no-cache-dir --break-system-packages \
    'jsonschema>=4.18,<5' 'langgraph>=1.2.16,<2' 'langgraph-checkpoint-sqlite>=3.1.2,<4'
ENTRYPOINT ["/bin/bash", "/adapter/scripts/ros_entrypoint.sh"]
