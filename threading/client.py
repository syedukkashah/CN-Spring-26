import socket 
import threading 

def server_comm():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect(('localhost', 9999))
    while True:
        msg = input('enter msg to send server (exit to end): ')
        if msg.lower() == 'exit':
            s.send(msg.encode())
            print('disconnecting...')
            s.close()
            break
        s.send(msg.encode())
        res = s.recv(1024).decode()
        print(f'received msg from server: {res}')


t = threading.Thread(target = server_comm)
t.start()
t.join()
