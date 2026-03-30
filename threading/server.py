import socket
import threading 

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(('localhost', 9999))
s.listen()
print('server listening on port 9999...')
def handle_client(client_socket):
    print(f'client [{client_socket.getpeername()}] being handled on thread {threading.current_thread().name}')
    while True:
        msg = client_socket.recv(1024)
        if msg.decode() == 'exit':
            print(f'closing connection with client [{client_socket.getpeername()}]')
            client_socket.close()
            break
        print(f'Received msg from client [{client_socket.getpeername()}]: {msg.decode()}')
        client_socket.send('received msg'.encode())
        


while True:
    c, addr = s.accept()
    t = threading.Thread(target = handle_client, args = (c,))
    t.start()
    
