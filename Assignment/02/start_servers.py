# runs all servers automatically
import subprocess
import sys
import time

scripts = [
    "root_server.py",
    "tld_servers/com.py",
    "tld_servers/edu.py",
    "tld_servers/org.py",
    "tld_servers/pk.py",
    "auth_servers/amazon.py",
    "auth_servers/google.py",
    "auth_servers/mit.py",
    "auth_servers/nu.py",
    "auth_servers/stanford.py",
    "auth_servers/wikipedia.py"
]

print("Starting all DNS servers...")
processes = []
for script in scripts:
    p = subprocess.Popen([sys.executable, script])
    processes.append(p)
    time.sleep(0.1) # brief pause to let them bind

print("All servers are running. You can now run the client.")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("Shutting down all servers...")
    for p in processes:
        p.terminate()
