``` c 
#include <iostream> 
#include <fstream> 
#include <string> 
#include "ns3/core-module.h" 
#include "ns3/network-module.h" 
#include "ns3/internet-module.h" 
#include "ns3/applications-module.h" 
#include "ns3/point-to-point-module.h" 
#include "ns3/ipv4-global-routing-helper.h" 
using namespace ns3; 
using namespace std; 
NS_LOG_COMPONENT_DEFINE ("TcpFlowVariant"); 
static const uint32_t TOTAL_DATA = 2000000; 
static const uint32_t CHUNK = 1040; 
static uint32_t sentSoFar = 0; 
uint8_t sendBuffer[CHUNK]; 
static ofstream fileOut; 
void InitiateConnection (Ptr<Socket>, Ipv4Address, uint16_t); 
void FillBuffer (Ptr<Socket>, uint32_t); 
static void 
TraceCwndChange (uint32_t prev, uint32_t curr) 
{ 
NS_LOG_INFO ("cwnd updated from " << prev << " to " << curr); 
fileOut << Simulator::Now ().GetSeconds () << "\t" << curr << "\n"; 
} 
int main (int argc, char *argv[]) 
{ 
fileOut.open ("cwnd-output.dat"); 
CommandLine args; 
args.Parse (argc, argv); 
 
  for (uint32_t i = 0; i < CHUNK; i++) 
  { 
    sendBuffer[i] = (uint8_t)(97 + (i % 26)); 
  } 
 
  NodeContainer group1; 
  group1.Create (2); 
 
  NodeContainer group2; 
  group2.Add (group1.Get (1)); 
  group2.Create (1); 
 
  PointToPointHelper p2pLink; 
  p2pLink.SetDeviceAttribute ("DataRate", StringValue ("10Mbps")); 
  p2pLink.SetChannelAttribute ("Delay", StringValue ("10ms")); 
 
  NetDeviceContainer link1 = p2pLink.Install (group1); 
  NetDeviceContainer link2 = p2pLink.Install (group2); 
 
  InternetStackHelper netStack; 
  netStack.InstallAll (); 
 
  Ipv4AddressHelper ipHelper; 
 
  ipHelper.SetBase ("10.1.3.0", "255.255.255.0"); 
  ipHelper.Assign (link1); 
 
  ipHelper.SetBase ("10.1.2.0", "255.255.255.0"); 
  Ipv4InterfaceContainer iface = ipHelper.Assign (link2); 
 
  Ipv4GlobalRoutingHelper::PopulateRoutingTables (); 
 
  uint16_t portNum = 50000; 
 
  PacketSinkHelper sinkAppHelper ("ns3::TcpSocketFactory", 
                                  InetSocketAddress (Ipv4Address::GetAny (), portNum)); 
 
  ApplicationContainer receiverApp = sinkAppHelper.Install (group2.Get (1)); 
  receiverApp.Start (Seconds (0.0)); 
  receiverApp.Stop (Seconds (3.0)); 
 
  Ptr<Socket> sock = 
    Socket::CreateSocket (group1.Get (0), TcpSocketFactory::GetTypeId ()); 
  sock->Bind (); 
 
  Config::ConnectWithoutContext ( 
    "/NodeList/0/$ns3::TcpL4Protocol/SocketList/0/CongestionWindow", 
    MakeCallback (&TraceCwndChange)); 
 
  Simulator::ScheduleNow (&InitiateConnection, sock, 
                          iface.GetAddress (1), portNum); 
 
  AsciiTraceHelper asciiHelper; 
  p2pLink.EnableAsciiAll (asciiHelper.CreateFileStream ("trace-output.tr")); 
  p2pLink.EnablePcapAll ("trace-output"); 
 
  Simulator::Stop (Seconds (1000)); 
  Simulator::Run (); 
  Simulator::Destroy (); 
 
  fileOut.close (); 
  return 0; 
} 
 
void InitiateConnection (Ptr<Socket> sock, 
                         Ipv4Address ip, 
                         uint16_t port) 
{ 
  sock->Connect (InetSocketAddress (ip, port)); 
  sock->SetSendCallback (MakeCallback (&FillBuffer)); 
  FillBuffer (sock, sock->GetTxAvailable ()); 
} 
 
void FillBuffer (Ptr<Socket> sock, uint32_t space) 
{ 
  while (sentSoFar < TOTAL_DATA && sock->GetTxAvailable () > 0) 
  { 
    uint32_t remaining = TOTAL_DATA - sentSoFar; 
    uint32_t index = sentSoFar % CHUNK; 
 
    uint32_t writeLen = CHUNK - index; 
 
    if (writeLen > remaining) 
      writeLen = remaining; 
 
    if (writeLen > sock->GetTxAvailable ()) 
      writeLen = sock->GetTxAvailable (); 
int result = sock->Send (&sendBuffer[index], writeLen, 0); 
if (result <= 0) 
break; 
sentSoFar += result; 
} 
sock->Close (); 
}

```

<img width="1520" height="802" alt="image" src="https://github.com/user-attachments/assets/2947af1e-611a-456e-ac55-1473c9ffba2d" />
