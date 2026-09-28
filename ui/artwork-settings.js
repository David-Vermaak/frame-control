(() => {
  const get=id=>document.getElementById(id), status=get('steamGridStatus');
  async function request(url,body) {
    const r=await fetch(url,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const data=await r.json(); if(!r.ok) throw Error(data.error||'Request failed'); return data;
  }
  function show(data) {
    status.textContent=data.environment?'SteamGridDB key is set by an environment variable.':
      data.steamgriddb_configured?'SteamGridDB key saved.':'No key configured. Source images and generated art are enabled.';
  }
  async function save(value) {
    try {show(await request('/api/settings/artwork',{steamgriddb_api_key:value}));get('steamGridKey').value='';}
    catch(e) {status.textContent=e.message;}
  }
  get('saveSteamGridKey').onclick=()=>save(get('steamGridKey').value.trim());
  get('clearSteamGridKey').onclick=()=>save('');
  get('refreshAndroidArt').onclick=async()=>{
    const button=get('refreshAndroidArt');button.disabled=true;
    try {
      const result=await runJob('Refresh Android artwork','android-artwork',()=>request('/api/android',{action:'refresh-art',all:true}));
      if (result) {
        const apps=Array.isArray(result.apps)?result.apps:[result.apps];
        const failed=apps.filter(a=>a.error);
        status.textContent=failed.length?`${failed.length} refresh failed: ${failed.map(a=>a.package+': '+a.error).join('; ')}`:
          `Refreshed artwork for ${apps.length} apps.`;
      }
    } catch(e) {status.textContent=e.message;}
    finally {button.disabled=false;}
  };
  request('/api/settings/artwork').then(show).catch(e=>{status.textContent=e.message;});
})();
