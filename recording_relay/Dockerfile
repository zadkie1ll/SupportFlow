FROM python:3.12-slim

WORKDIR /app
COPY recording_relay/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY recording_relay ./recording_relay

USER 65534:65534
EXPOSE 8055
CMD ["uvicorn", "recording_relay.app:app", "--host", "0.0.0.0", "--port", "8055"]

