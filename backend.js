
const http = require('http');

const url = 'https://jsonplaceholder.typicode.com/posts/1';
https.get(url, (res) => {
   let data = '';
   // Collect data chunks
   res.on('data', (chunk) => {
       data += chunk;
   });
   // Parse and log the response when complete
   res.on('end', () => {
       console.log(JSON.parse(data));
   });
}).on('error', (err) => {
   console.error('Error:', err.message);
});

const server = http.createServer((req, res) => {
  console.log("req recieved");
  if (req.method === 'GET' && req.url === '/data') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ message: 'Hello from Node!' }));
  } else {
    res.writeHead(404);
    res.end('Not Found');
  }
});

server.listen(3000, () => {
  console.log('Server running at http://localhost:3000');
});