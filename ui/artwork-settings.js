// Uses index.html's api(), which sends the X-Frame-UI key every /api call needs.
(() => {
  const get=id=>document.getElementById(id), status=get('steamGridStatus');
  function show(data) {
    status.textContent=data.environment?'SteamGridDB key is set by an environment variable.':
      data.steamgriddb_configured?'SteamGridDB key saved.':'No key configured. Source images and generated art are enabled.';
  }
  async function save(value) {
    try {show(await api('/api/settings/artwork',{steamgriddb_api_key:value}));get('steamGridKey').value='';}
    catch(e) {status.textContent=e.message;}
  }
  get('saveSteamGridKey').onclick=()=>save(get('steamGridKey').value.trim());
  get('clearSteamGridKey').onclick=()=>save('');
  get('refreshAndroidArt').onclick=async()=>{
    const button=get('refreshAndroidArt');button.disabled=true;
    try {
      const result=await runJob('Refresh library artwork','library-artwork',()=>api('/api/android',{action:'refresh-art',all:true}));
      if (result) {
        const items=[...(result.apps||[]),...(result.titles||[])];
        const failed=items.filter(a=>a.error);
        status.textContent=failed.length?`${failed.length} of ${items.length} failed: ${failed.map(a=>(a.package||a.id)+': '+a.error).join('; ')}`:
          `Refreshed artwork for ${items.length} apps and titles.`;
        if (typeof loadAndroid==='function') loadAndroid();
        if (typeof loadTitles==='function') loadTitles();
      }
    } catch(e) {status.textContent=e.message;}
    finally {button.disabled=false;}
  };
  api('/api/settings/artwork').then(show).catch(e=>{status.textContent=e.message;});
})();
