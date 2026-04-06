import socket
import json

ROOT_PORT = 5000

tld_servers = {
    "com": ("127.0.0.1", 5001),
    "org": ("127.0.0.1", 5002),
    "edu": ("127.0.0.1", 5003),
    "pk": ("127.0.0.1", 5004)
}

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("127.0.0.1", ROOT_PORT))

print("Root DNS running on port", ROOT_PORT)

while True:
    data, addr = sock.recvfrom(1024)
    request = json.loads(data.decode())

    domain = request["domain"]
    tld = domain.split(".")[-1]

    if tld in tld_servers:
        request = json.loads(data.decode())
        response = {
            "id": request["id"],
            "type": "RESPONSE",
            "server": tld_servers[tld]
        }
        
    else:
        response = {"error": "Unknown TLD"}

    sock.sendto(json.dumps(response).encode(), addr)