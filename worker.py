import os
import redis
from rq import Worker, Queue
from app import app, redis_conn, transcribe_video_task, burn_subtitles_task

# Preload Flask app context for db access within tasks
with app.app_context():
    if __name__ == '__main__':
        # Define the queue(s) to listen to
        queues = [Queue(connection=redis_conn)]  # Explicitly pass the redis_conn
        worker = Worker(queues, connection=redis_conn)
        worker.work()
