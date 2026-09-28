const fs=require('fs'),vm=require('vm'),assert=require('assert');
const stub=require(process.cwd()+'/tests/fakeframe/rootfs/usr/local/lib/fakeframe/canvas_stub');
(async()=>{
 const surface=stub.surface(),ctx=vm.createContext(surface);
 vm.runInContext(fs.readFileSync('frame/android/library_artwork.js','utf8'),ctx);
 const result=await ctx.renderLibraryArtwork({label:'Example Game',images:{icon:['png','fixture']}});
 assert.deepEqual(Object.keys(result.images),['grid','wide','hero','logo','icon']);
 for(const [slot,size] of Object.entries({grid:[600,900],wide:[920,430],hero:[3840,1240],logo:[1280,480],icon:[256,256]})) {
  const b=Buffer.from(result.images[slot],'base64');assert.equal(b.readUInt32BE(16),size[0]);assert.equal(b.readUInt32BE(20),size[1]);
 }
 const hero=surface.canvases.find(c=>c.width===3840);assert.equal(hero.text.length,0);
 const logo=surface.canvases.find(c=>c.width===1280);assert(logo.text.length);assert.equal(logo.draws,0);
 const before=surface.canvases.length;await ctx.renderLibraryArtwork({label:'No Icon',images:{}});
 assert.equal(surface.canvases.slice(before).find(c=>c.width===3840).text.length,0);
 console.log('five dimensions, textless hero, title logo: OK');
})().catch(e=>{console.error(e);process.exit(1)});
