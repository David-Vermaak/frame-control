// Runs in Steam's Chromium context: identical fonts/rendering from every host OS.
async function renderLibraryArtwork(input) {
  const sizes = {grid:[600,900], wide:[920,430], hero:[3840,1240], logo:[1280,480], icon:[256,256]};
  const label = String(input.label || 'Untitled').trim().slice(0,180);
  const font = '"Motiva Sans", "Noto Sans", Arial, sans-serif';
  await document.fonts.load(`800 120px ${font}`, label);
  const images = {}, warnings = [];
  for (const [slot, item] of Object.entries(input.images || {})) {
    try {
      const img = new Image();
      img.src = `data:image/${item[0]};base64,${item[1]}`;
      await img.decode();
      if (!img.width || !img.height || img.width*img.height > 16777216) throw Error('dimensions');
      images[slot] = img;
    } catch (_) { warnings.push(`${slot} could not be decoded; generated art used`); }
  }
  let icon = images.icon;
  if (icon) {
    // Remove only a near-black matte connected to the outside of an opaque icon.
    const cut=document.createElement('canvas');cut.width=icon.width;cut.height=icon.height;
    const c=cut.getContext('2d');c.drawImage(icon,0,0);
    const pixels=c.getImageData(0,0,cut.width,cut.height), d=pixels.data, w=cut.width,h=cut.height;
    const corners=[0,w-1,(h-1)*w,h*w-1];
    if(corners.every(i=>d[i*4+3]>250 && Math.max(d[i*4],d[i*4+1],d[i*4+2])<24)) {
      const seen=new Uint8Array(w*h), queue=corners.slice();
      for(let q=0;q<queue.length;q++) {
        const i=queue[q];if(seen[i])continue;seen[i]=1;
        if(Math.max(d[i*4],d[i*4+1],d[i*4+2])>24)continue;
        d[i*4+3]=0;
        if(i%w)queue.push(i-1);if(i%w<w-1)queue.push(i+1);
        if(i>=w)queue.push(i-w);if(i<w*(h-1))queue.push(i+w);
      }
      c.putImageData(pixels,0,0);icon=cut;
    }
  }
  // Quantized, saturated dominant colors avoid white/black icon margins.
  let colors = [[48,91,137], [24,36,63]];
  if (icon) {
    const sample = document.createElement('canvas'); sample.width=48; sample.height=48;
    const s=sample.getContext('2d'); s.drawImage(icon,0,0,48,48);
    const data=s.getImageData(0,0,48,48).data, bins=new Map();
    for (let i=0;i<data.length;i+=4) {
      const rgb=[data[i],data[i+1],data[i+2]], hi=Math.max(...rgb), lo=Math.min(...rgb);
      if (data[i+3]<150 || hi<45 || lo>220 || hi-lo<25) continue;
      const key=rgb.map(v=>Math.round(v/32)*32).join(',');
      bins.set(key,(bins.get(key)||0)+1);
    }
    const ranked=[...bins].sort((a,b)=>b[1]-a[1]);
    if (ranked.length) {
      colors[0]=ranked[0][0].split(',').map(Number);
      colors[1]=(ranked.find(([key])=>key.split(',').reduce((n,v,i)=>n+Math.abs(Number(v)-colors[0][i]),0)>170)||ranked[0])[0].split(',').map(Number);
    }
  }
  // Preserve hue while lifting muted icon colors into a richer background palette.
  colors=colors.map(c=>{const low=Math.min(...c),range=Math.max(...c)-low||1;
    return c.map(v=>45+(v-low)/range*165);});
  const rgb=(c,a=1)=>`rgba(${c.map(v=>Math.min(255,Math.round(v))).join(',')},${a})`;
  function image(ctx,img,x,y,w,h,cover=false) {
    const scale=cover?Math.max(w/img.width,h/img.height):Math.min(w/img.width,h/img.height);
    const dw=img.width*scale,dh=img.height*scale;
    ctx.save(); ctx.beginPath(); ctx.rect(x,y,w,h); ctx.clip();
    ctx.drawImage(img,x+(w-dw)/2,y+(h-dh)/2,dw,dh); ctx.restore();
  }
  function title(ctx,w,h,top,bottom,maxSize) {
    let lines=[],size=maxSize;
    const maxWidth=w*.84;
    for (;size>=18;size-=2) {
      ctx.font=`800 ${size}px ${font}`;
      lines=[]; let line='';
      for (const word of label.split(/\s+/)) {
        const next=line?line+' '+word:word;
        if (line && ctx.measureText(next).width>maxWidth) {lines.push(line);line=word;} else line=next;
      }
      lines.push(line);
      if (lines.length*size*1.08<=bottom-top && lines.every(l=>ctx.measureText(l).width<=maxWidth)) break;
    }
    if(lines.length===2) {
      const words=lines[0].split(' ');
      if(words.length>1) {
        const first=words.slice(0,-1).join(' '), second=words.slice(-1)[0]+' '+lines[1];
        if(ctx.measureText(second).width<=maxWidth &&
           Math.abs(ctx.measureText(first).width-ctx.measureText(second).width)<
           Math.abs(ctx.measureText(lines[0]).width-ctx.measureText(lines[1]).width)) lines=[first,second];
      }
    }
    // A long unbroken label is still fitted, including scripts without spaces.
    ctx.textAlign='center'; ctx.textBaseline='middle'; ctx.fillStyle='#fff';
    ctx.shadowColor='rgba(0,0,0,.45)'; ctx.shadowBlur=size*.28; ctx.shadowOffsetY=size*.06;
    let y=top+(bottom-top-lines.length*size*1.08)/2+size*.54;
    for (const line of lines) {ctx.fillText(line,w/2,y,maxWidth); y+=size*1.08;}
    ctx.shadowBlur=0; ctx.shadowOffsetY=0;
  }
  const result={};
  for (const [slot,[w,h]] of Object.entries(sizes)) {
    const canvas=document.createElement('canvas'); canvas.width=w; canvas.height=h;
    const ctx=canvas.getContext('2d'); ctx.imageSmoothingQuality='high';
    const direct=images[slot];
    const feature=images.feature_graphic||images.banner;
    const scene=direct || ((slot==='hero'||slot==='wide') && (feature||images.screenshot));
    if (scene) {
      if (slot==='logo'||slot==='icon') image(ctx,scene,0,0,w,h);
      else image(ctx,scene,0,0,w,h,true);
    } else if (slot==='logo') {
      title(ctx,w,h,h*.08,h*.92,150);
    } else {
      const gradient=ctx.createLinearGradient(0,0,w,h);
      gradient.addColorStop(0,rgb(colors[0].map(v=>v*.68)));
      gradient.addColorStop(.6,rgb(colors[1].map(v=>v*.32)));
      gradient.addColorStop(1,'#080c16'); ctx.fillStyle=gradient;ctx.fillRect(0,0,w,h);
      if (icon) {
        ctx.save();ctx.globalAlpha=.16;ctx.filter=`blur(${Math.round(w*.055)}px) saturate(1.4)`;
        image(ctx,icon,-w*.15,-h*.15,w*1.3,h*1.3,true);ctx.restore();
      }
      const glow=ctx.createRadialGradient(w*.5,h*.32,0,w*.5,h*.32,w*.8);
      glow.addColorStop(0,rgb(colors[0],.27));glow.addColorStop(1,rgb(colors[1],0));
      ctx.fillStyle=glow;ctx.fillRect(0,0,w,h);
      const vignette=ctx.createLinearGradient(0,h*.25,0,h);
      vignette.addColorStop(0,'rgba(0,0,0,0)');vignette.addColorStop(1,'rgba(0,0,0,.56)');
      ctx.fillStyle=vignette;ctx.fillRect(0,0,w,h);
      const box=slot==='grid'?[w*.12,h*.14,w*.76,w*.76]:
                slot==='wide'?[w*.36,h*.06,w*.28,h*.59]:
                slot==='hero'?[w*.365,h*.12,w*.27,h*.78]:[w*.08,h*.08,w*.84,h*.84];
      if (icon) {
        ctx.save();ctx.shadowColor='rgba(0,0,0,.65)';ctx.shadowBlur=Math.min(w,h)*.055;
        ctx.shadowOffsetY=Math.min(w,h)*.022;
        // Opaque square icons read as deliberate app tiles, not pasted rectangles.
        const [x,y,bw,bh]=box, side=Math.min(bw,bh);
        if(slot!=='hero' && icon===images.icon) {
          ctx.beginPath();ctx.roundRect(x+(bw-side)/2,y+(bh-side)/2,side,side,side*.14);ctx.clip();
        }
        image(ctx,icon,...box);ctx.restore();
      } else if (slot !== 'hero') {
        // A typographic monogram when the APK contains no usable image.
        ctx.font=`800 ${Math.min(w,h)*.48}px ${font}`;ctx.fillStyle='rgba(255,255,255,.94)';
        ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText([...label][0]||'A',w/2,h*.38);
      }
      if (slot==='grid') title(ctx,w,h,h*.7,h*.93,66);
      if (slot==='wide') title(ctx,w,h,h*.69,h*.92,52);
      // Hero intentionally has no title: Steam overlays the transparent logo.
    }
    // Photos as PNG can pass Steam's 12 MiB limit at hero size; the logo keeps its transparency.
    const jpeg=scene && slot!=='logo' && slot!=='icon';
    result[slot]=[jpeg?'jpg':'png', canvas.toDataURL(jpeg?'image/jpeg':'image/png',.9).split(',')[1]];
  }
  return {images:result,warnings,font};
}
