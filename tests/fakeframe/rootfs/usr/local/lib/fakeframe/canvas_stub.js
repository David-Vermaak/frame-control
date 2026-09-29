// Synthetic canvas for API-path tests only. It emits transparent PNGs, not visual proof.
const zlib = require('zlib');
function crc(data) {
  let c=0xffffffff;
  for(const b of data) {c^=b;for(let i=0;i<8;i++)c=(c>>>1)^((c&1)?0xedb88320:0);}
  return (c^0xffffffff)>>>0;
}
function chunk(name,data) {
  const body=Buffer.concat([Buffer.from(name),data]), n=Buffer.alloc(4), sum=Buffer.alloc(4);
  n.writeUInt32BE(data.length);sum.writeUInt32BE(crc(body));return Buffer.concat([n,body,sum]);
}
function png(w,h) {
  const header=Buffer.alloc(13);header.writeUInt32BE(w);header.writeUInt32BE(h,4);header[8]=8;header[9]=6;
  return Buffer.concat([Buffer.from('89504e470d0a1a0a','hex'),chunk('IHDR',header),
    chunk('IDAT',zlib.deflateSync(Buffer.alloc((w*4+1)*h))),chunk('IEND',Buffer.alloc(0))]).toString('base64');
}
function surface() {
  const canvases=[];
  const document={fonts:{load:async()=>[]},createElement(tag) {
    if(tag!=='canvas')throw Error('unexpected element');
    const canvas={width:1,height:1,text:[],draws:0};
    const ctx={measureText:t=>({width:String(t).length*30}),fillText(t){canvas.text.push(t);},
      drawImage(){canvas.draws++;},getImageData:()=>({data:new Uint8ClampedArray(canvas.width*canvas.height*4)}),
      createLinearGradient:()=>({addColorStop(){}}),createRadialGradient:()=>({addColorStop(){}})};
    for(const method of ['save','restore','beginPath','rect','roundRect','clip','fillRect','putImageData','arc','fill','stroke'])ctx[method]=()=>{};
    canvas.getContext=()=>ctx;canvas.toDataURL=type=>type==='image/jpeg'?'data:image/jpeg;base64,'+Buffer.from('ffd8ffe000104a464946','hex').toString('base64'):
      'data:image/png;base64,'+png(canvas.width,canvas.height);
    canvases.push(canvas);return canvas;
  }};
  class Image {constructor(){this.width=2;this.height=2;} async decode(){if(this.src.includes('YmFk'))throw Error('bad image');}}
  return {document,Image,canvases};
}
module.exports={surface};
