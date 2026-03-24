(function () {
  function $(id){return document.getElementById(id);}
  function fmtKm(m){return(m/1000).toFixed(1)+" km";}
  function fmtMin(s){return Math.max(1,Math.round(s/60))+" min";}
  function safetyPct(v){return Math.round(v);}
  function zoneColor(z){return z==="safe"?"#29ff9a":z==="moderate"?"#ffd35a":"#ff4d6d";}
  function routeColor(k){return k==="Safest Route"?"#20e37f":k==="Balanced Route"?"#e3b62f":k==="Fastest Route"?"#e33456":"#20e37f";}
  function routeIcon(k){return k==="Safest Route"?"🛡️":k==="Balanced Route"?"⚖️":k==="Fastest Route"?"⚡":"📍";}
  function debounce(fn,ms){let t;return function(...a){clearTimeout(t);t=setTimeout(()=>fn.apply(this,a),ms);};}
  function toRad(d){return d*Math.PI/180;}

  function haversineM(aLat,aLng,bLat,bLng){
    const R=6371000,dLat=toRad(bLat-aLat),dLng=toRad(bLng-aLng);
    const s1=Math.sin(dLat/2),s2=Math.sin(dLng/2);
    const a=s1*s1+Math.cos(toRad(aLat))*Math.cos(toRad(bLat))*s2*s2;
    return 2*R*Math.atan2(Math.sqrt(a),Math.sqrt(1-a));
  }
  function buildCumDist(cl){const c=[0];for(let i=1;i<cl.length;i++)c.push(c[i-1]+haversineM(cl[i-1][0],cl[i-1][1],cl[i][0],cl[i][1]));return c;}
  function nearestIndex(cl,lat,lng){
    let bI=0,bD=Infinity;
    const step=cl.length>1200?6:cl.length>600?4:cl.length>250?2:1;
    for(let i=0;i<cl.length;i+=step){const d=haversineM(cl[i][0],cl[i][1],lat,lng);if(d<bD){bD=d;bI=i;}}
    const s=Math.max(0,bI-step*2),e=Math.min(cl.length-1,bI+step*2);
    for(let i=s;i<=e;i++){const d=haversineM(cl[i][0],cl[i][1],lat,lng);if(d<bD){bD=d;bI=i;}}
    return{idx:bI,distM:bD};
  }

  const state={
    start:null,end:null,lastClick:null,heatEnabled:true,
    routes:[],aiBest:null,selectedRouteKey:"Safest Route",
    navigating:false,watchId:null,
    nav:{active:null,coordsLatLng:[],cumDistM:[],startedAtMs:null,lastRerouteAtMs:0,traveledIdx:0,arrivedFired:false},
  };

  const isMobile=()=>window.innerWidth<=760;
  const ui={
    status:$("status"),cards:$("route-cards"),incidents:$("incident-list"),
    bnCurrent:$("bn-current")||{textContent:""},bnDest:$("bn-dest")||{textContent:""},
    bnReco:$("bn-reco"),bnEta:$("bn-eta"),bnRemaining:$("bn-remaining"),
    bnSpeed:$("bn-speed")||{textContent:""},bnNext:$("bn-next"),bnScore:$("bn-score"),
    get inputStart(){return isMobile()&&$("mob-input-start")?$("mob-input-start"):$("input-current");},
    get inputEnd()  {return isMobile()&&$("mob-input-dest")?$("mob-input-dest"):$("input-dest");},
    get sugStart(){return isMobile()&&$("mob-suggest-start")?$("mob-suggest-start"):$("suggest-current");},
    get sugEnd()  {return isMobile()&&$("mob-suggest-dest")?$("mob-suggest-dest"):$("suggest-dest");},
    navBanner:$("nav-banner"),navArrow:$("nav-arrow"),navInstruction:$("nav-instruction"),
    navDistNext:$("nav-dist-next"),navRemaining:$("nav-remaining"),navEta:$("nav-eta"),
    navSpeed:$("nav-speed"),navSafety:$("nav-safety"),
    progressBar:$("route-progress-bar"),
    arrivedOverlay:$("arrived-overlay"),arrivedDest:$("arrived-dest"),
  };

  let lastNavPos=null,lastNavTime=null;
  function setStatus(msg){if(ui.status)ui.status.textContent=msg;}

  /* ── FIX 1: Track map load state so applyPosition is safe to call anytime ── */
  let mapLoaded = false;
  let pendingPosition = null; // stores {lat,lng,speed} if GPS fires before map loads

  /* ── FIX 2: applyPosition — now safe before map loads, syncs all inputs ── */
  function applyPosition(lat, lng, speed) {
    // If map isn't ready yet, queue the position and apply it once map loads
    if (!mapLoaded) {
      pendingPosition = { lat, lng, speed };
      return;
    }
    if (!state.start || state.start.label === "Current Location") {
      state.start = { lat, lng, label: "Current Location" };
      // Sync ALL four input fields — desktop + mobile
      const desktopInp = $("input-current");
      const mobileInp  = $("mob-input-start");
      if (desktopInp) desktopInp.value = "📍 Current Location";
      if (mobileInp)  mobileInp.value  = "📍 Current Location";
      setStartMarker([lat, lng], "📍 Current Location");
      setStatus("✅ Live location detected.");
      map.flyTo({ center: [lng, lat], zoom: 15, duration: 800 });
      // Remove the manual locate button if visible
      const btn = $("btn-locate-me");
      if (btn) btn.remove();
    }
    if (state.navigating && state.nav.active) updateNavigation(lat, lng, speed);
  }

  /* Map */
  const map=new maplibregl.Map({
    container:"map",
    style:{version:8,glyphs:"https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
      sources:{osm:{type:"raster",tiles:["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],tileSize:256,attribution:"© OpenStreetMap contributors"}},
      layers:[{id:"osm-tiles",type:"raster",source:"osm"}]},
    center:[80.6480,16.5062],zoom:13,
  });
  map.addControl(new maplibregl.NavigationControl({showCompass:true}),"bottom-right");

  let startMarker=null,endMarker=null,navDot=null;
  let routeSrcIds=[],routeLyrIds=[],arrowLyr="route-arrows";
  const hmSrc="safety-heatmap",hmLyr="safety-heatmap-layer";
  const spSrc="safety-points",spClust="safety-clusters",spPts="safety-points-layer",spCnt="safety-cluster-count";

  function createNavDotEl(){
    const wrap=document.createElement("div");wrap.style.cssText="position:relative;width:22px;height:22px;";
    const ring=document.createElement("div");
    ring.style.cssText="position:absolute;inset:-6px;border-radius:50%;background:rgba(32,227,127,0.2);animation:navPulse 1.5s ease-in-out infinite;";
    const dot=document.createElement("div");
    dot.style.cssText="width:22px;height:22px;border-radius:50%;background:#20e37f;border:3px solid #fff;box-shadow:0 2px 8px rgba(0,0,0,0.4);";
    wrap.appendChild(ring);wrap.appendChild(dot);
    if(!document.getElementById("nav-pulse-style")){
      const s=document.createElement("style");s.id="nav-pulse-style";
      s.textContent="@keyframes navPulse{0%,100%{transform:scale(1);opacity:0.6}50%{transform:scale(1.4);opacity:0.2}}";
      document.head.appendChild(s);
    }
    return wrap;
  }

  function clearRoutes(){
    routeLyrIds.forEach(id=>{if(map.getLayer(id))map.removeLayer(id);});routeLyrIds=[];
    routeSrcIds.forEach(id=>{if(map.getSource(id))map.removeSource(id);});routeSrcIds=[];
    if(map.getLayer(arrowLyr))map.removeLayer(arrowLyr);if(map.getSource("route-arrows"))map.removeSource("route-arrows");
    if(map.getLayer("route-traveled"))map.removeLayer("route-traveled");if(map.getSource("route-traveled"))map.removeSource("route-traveled");
  }

  function setStartMarker(ll,label){
    if(startMarker)startMarker.remove();
    const el=document.createElement("div");Object.assign(el.style,{width:"16px",height:"16px",borderRadius:"50%",backgroundColor:"#45a3ff",border:"3px solid #fff",boxShadow:"0 2px 6px rgba(0,0,0,0.4)"});
    startMarker=new maplibregl.Marker({element:el}).setLngLat(Array.isArray(ll)?[ll[1],ll[0]]:[ll.lng,ll.lat]).setPopup(new maplibregl.Popup({offset:15}).setHTML(label||"Start")).addTo(map);
  }
  function setEndMarker(ll,label){
    if(endMarker)endMarker.remove();
    const el=document.createElement("div");Object.assign(el.style,{width:"18px",height:"18px",borderRadius:"50%",backgroundColor:"#ff4d6d",border:"3px solid #fff",boxShadow:"0 2px 6px rgba(0,0,0,0.4)"});
    endMarker=new maplibregl.Marker({element:el}).setLngLat(Array.isArray(ll)?[ll[1],ll[0]]:[ll.lng,ll.lat]).setPopup(new maplibregl.Popup({offset:15}).setHTML(label||"Destination")).addTo(map);
  }
  function fitToStartEnd(){if(!state.start||!state.end)return;map.fitBounds([[state.start.lng,state.start.lat],[state.end.lng,state.end.lat]],{padding:80,duration:400});}

  map.on("load",()=>{
    /* ── FIX 3: Set mapLoaded=true FIRST, then flush any queued GPS position ── */
    mapLoaded = true;
    if (pendingPosition) {
      const { lat, lng, speed } = pendingPosition;
      pendingPosition = null;
      applyPosition(lat, lng, speed);
    }

    /* ── FIX 4: sp:locationGranted listener is now OUTSIDE map.on("load")
       (see below) so this block is removed from here entirely ── */

    map.on("click",e=>{
      state.lastClick={lat:e.lngLat.lat,lng:e.lngLat.lng};
      if(!state.start){
        state.start={lat:e.lngLat.lat,lng:e.lngLat.lng,label:"Start (map click)"};
        ui.inputStart.value="Current Location (map click)";
        setStartMarker([e.lngLat.lat,e.lngLat.lng],"Start");
      } else if(!state.end){
        state.end={lat:e.lngLat.lat,lng:e.lngLat.lng,label:"Destination (map click)"};
        ui.inputEnd.value="Destination (map click)";
        setEndMarker([e.lngLat.lat,e.lngLat.lng],"Destination");
        fitToStartEnd();
      }
    });

    (async function boot(){
      try{
        setStatus("Loading safety dataset…");
        await loadSafetyPoints();
        setStatus("Ready. Detecting live location…");
        /* ── FIX 5: detectLiveLocation called here after map+data ready ── */
        detectLiveLocation();
      }catch(e){
        setStatus("Failed to initialize.");
        console.error(e);
      }
    })();
  });

  /* ── FIX 4 (continued): Register sp:locationGranted OUTSIDE map.on("load")
     so it's listening even if GPS responds before the map finishes loading.
     applyPosition now safely queues the position if map isn't ready yet. ── */
  document.addEventListener("sp:locationGranted", e => {
    const { lat, lng } = e.detail;
    applyPosition(lat, lng, null);
  });

  function safetyPctPt(p){
    const raw=0.25*Number(p.street_lighting??5)+0.15*Number(p.crowd_density??5)+0.10*Number(p.police_proximity??5)+0.10*Number(p.cctv_coverage??5)+0.10*Number(p.road_visibility??5)+0.10*Number(p.traffic_density??5)-0.15*Number(p.crime_rate??5)-0.05*Number(p.incident_reports??3);
    return Math.round(Math.max(0,Math.min(100,((raw+1.2)/9.05)*100)));
  }
  function safetyBand(pct){if(pct>=70)return{band:"safe",color:"#29ff9a"};if(pct>=40)return{band:"moderate",color:"#ffd35a"};return{band:"unsafe",color:"#ff4d6d"};}

  async function loadSafetyPoints(){
    const res=await fetch("/api/safety_points"),data=await res.json();
    const pF=[],hF=[];
    data.points.forEach(p=>{
      const pct=typeof p.safety_percent==="number"?Math.round(p.safety_percent):safetyPctPt(p),band=safetyBand(pct);
      pF.push({type:"Feature",geometry:{type:"Point",coordinates:[p.lng,p.lat]},properties:{color:band.color,pct,band:band.band,area:p.area||p.name||"Location #"+(p.id??""),crime_rate:p.crime_rate??"—",street_lighting:p.street_lighting??"—",crowd_density:p.crowd_density??"—"}});
      hF.push({type:"Feature",geometry:{type:"Point",coordinates:[p.lng,p.lat]},properties:{intensity:Math.max(0.05,(100-pct)/100)}});
    });
    [spCnt,spPts,spClust].forEach(id=>{if(map.getLayer(id))map.removeLayer(id);});if(map.getSource(spSrc))map.removeSource(spSrc);
    map.addSource(spSrc,{type:"geojson",data:{type:"FeatureCollection",features:pF},cluster:true,clusterMaxZoom:14,clusterRadius:45});
    map.addLayer({id:spClust,type:"circle",source:spSrc,filter:["has","point_count"],paint:{"circle-color":"#6cf6ff","circle-radius":["step",["get","point_count"],18,10,22,30,26],"circle-stroke-width":1,"circle-stroke-color":"#e8efff"}});
    map.addLayer({id:spCnt,type:"symbol",source:spSrc,filter:["has","point_count"],layout:{"text-field":["get","point_count_abbreviated"],"text-font":["DIN Offc Pro Medium","Arial Unicode MS Bold"],"text-size":12},paint:{"text-color":"#05060b"}});
    map.addLayer({id:spPts,type:"circle",source:spSrc,filter:["!",["has","point_count"]],paint:{"circle-color":["get","color"],"circle-radius":7,"circle-stroke-width":2,"circle-stroke-color":"#ffffff"}});
    map.on("click",spClust,e=>{const f=map.queryRenderedFeatures(e.point,{layers:[spClust]});if(!f.length)return;map.getSource(spSrc).getClusterExpansionZoom(f[0].properties.cluster_id,(err,zoom)=>{if(!err)map.flyTo({center:f[0].geometry.coordinates,zoom});});});
    map.on("click",spPts,e=>{const p=e.features[0].properties,c=e.features[0].geometry.coordinates.slice();new maplibregl.Popup().setLngLat(c).setHTML(`<b>${p.area}</b><br/>Safety: <b>${p.pct}%</b> (${p.band})<br/>Crime: ${p.crime_rate}<br/>Lighting: ${p.street_lighting}<br/>Crowd: ${p.crowd_density}`).addTo(map);});
    [spClust,spPts].forEach(id=>{map.on("mouseenter",id,()=>{map.getCanvas().style.cursor="pointer";});map.on("mouseleave",id,()=>{map.getCanvas().style.cursor="";});});
    if(map.getSource(hmSrc))map.removeSource(hmSrc);if(map.getLayer(hmLyr))map.removeLayer(hmLyr);
    map.addSource(hmSrc,{type:"geojson",data:{type:"FeatureCollection",features:hF}});
    map.addLayer({id:hmLyr,type:"heatmap",source:hmSrc,maxzoom:17,paint:{"heatmap-weight":["get","intensity"],"heatmap-intensity":1,"heatmap-color":["interpolate",["linear"],["heatmap-density"],0,"rgba(0,0,0,0)",0.2,"rgba(255,77,109,0.3)",0.5,"rgba(255,211,90,0.5)",0.8,"rgba(41,255,154,0.6)",1,"rgba(108,246,255,0.8)"],"heatmap-radius":28,"heatmap-opacity":state.heatEnabled?0.7:0}},spClust);
  }

  /* ══════════════════════════════════════════════════════════
     FIX 6: Completely rewritten detectLiveLocation()
     Root causes fixed:
       A) Android Chrome requires permissions-policy header OR
          a user-gesture for high-accuracy GPS — we try low
          accuracy first (network/WiFi) which doesn't need a
          gesture and works instantly, then upgrade to GPS.
       B) maximumAge:0 caused constant GPS cold-start timeouts
          on Android — changed to 30000ms.
       C) Timeout errors (code 3) now show the manual button,
          not just permission-denied errors (code 1).
  ══════════════════════════════════════════════════════════ */
  function detectLiveLocation(){
    if(!navigator.geolocation){
      setStatus("Geolocation not supported. Enter location manually.");
      showLocationButton();
      return;
    }

    // Clear any existing watch
    if(state.watchId !== null){
      try{ navigator.geolocation.clearWatch(state.watchId); }catch(_){}
      state.watchId = null;
    }

    let gotFix = false;

    function onSuccess(pos){
      gotFix = true;
      applyPosition(pos.coords.latitude, pos.coords.longitude, pos.coords.speed);
    }

    function onDenied(){
      setStatus("📍 Location denied. Enter start location manually.");
      document.dispatchEvent(new CustomEvent("sp:locationDenied"));
    }

    /* ── STEP 1: Fast coarse fix via WiFi/cell towers
       enableHighAccuracy:false = no GPS satellite needed
       Works instantly on Android Chrome, no permission prompt needed
       beyond the basic location permission ── */
    navigator.geolocation.getCurrentPosition(
      onSuccess,
      err => {
        if(err.code === 1){ onDenied(); return; }
        // Timeout or unavailable — try once more relaxed
        navigator.geolocation.getCurrentPosition(
          onSuccess,
          err2 => {
            if(err2.code === 1){ onDenied(); return; }
            // Still failed — show manual button, don't block user
            setStatus("📍 Location unavailable. Tap button or type manually.");
            showLocationButton();
          },
          { enableHighAccuracy:false, timeout:20000, maximumAge:120000 }
        );
      },
      { enableHighAccuracy:false, timeout:8000, maximumAge:60000 }
    );

    /* ── STEP 2: Precise GPS watch (refines after coarse fix)
       maximumAge:30000 = accept cached GPS up to 30s old
       This avoids the Android "GPS cold start" timeout loop ── */
    state.watchId = navigator.geolocation.watchPosition(
      onSuccess,
      err => {
        if(err.code === 1){ onDenied(); return; }
        if(!gotFix){
          // Timeout on first watch attempt — show manual button
          showLocationButton();
        }
      },
      { enableHighAccuracy:true, timeout:30000, maximumAge:30000 }
    );

    /* ── STEP 3: Fallback — if no fix at all after 10 seconds,
       show the manual locate button so user isn't stuck ── */
    setTimeout(()=>{
      if(!gotFix && !state.start){
        setStatus("📍 GPS slow — tap button to retry or type manually.");
        showLocationButton();
      }
    }, 10000);
  }

  /* ── FIX 7: showLocationButton — bigger, better positioned,
     works as a user-gesture trigger for GPS on Android ── */
  function showLocationButton(){
    if($("btn-locate-me")) return; // already showing

    const btn = document.createElement("button");
    btn.id = "btn-locate-me";
    btn.innerHTML = "📍 Detect My Location";
    btn.style.cssText = [
      "position:fixed",
      "bottom:90px",
      "left:50%",
      "transform:translateX(-50%)",
      "z-index:9999",
      "background:#20e37f",
      "color:#05060b",
      "border:none",
      "border-radius:24px",
      "padding:13px 28px",
      "font-weight:800",
      "font-size:1rem",
      "cursor:pointer",
      "box-shadow:0 4px 20px rgba(0,0,0,0.45)",
      "white-space:nowrap",
      "letter-spacing:0.01em",
    ].join(";");

    btn.addEventListener("click", ()=>{
      btn.innerHTML = "📍 Detecting…";
      btn.disabled = true;

      // This click IS a user gesture — Android will allow GPS here
      navigator.geolocation.getCurrentPosition(
        pos => {
          applyPosition(pos.coords.latitude, pos.coords.longitude, pos.coords.speed);
          btn.remove();
          // Start ongoing watch now that user has confirmed permission
          if(state.watchId !== null){
            try{ navigator.geolocation.clearWatch(state.watchId); }catch(_){}
          }
          state.watchId = navigator.geolocation.watchPosition(
            p => applyPosition(p.coords.latitude, p.coords.longitude, p.coords.speed),
            err => { if(err.code === 1){ btn.remove(); } },
            { enableHighAccuracy:true, timeout:30000, maximumAge:30000 }
          );
        },
        err => {
          btn.innerHTML = "📍 Detect My Location";
          btn.disabled = false;
          if(err.code === 1){
            setStatus("Location denied. Enter start location manually.");
            btn.remove();
            document.dispatchEvent(new CustomEvent("sp:locationDenied"));
          } else {
            // Timeout — let them try again
            setStatus("GPS timed out. Try again or type manually.");
          }
        },
        { enableHighAccuracy:true, timeout:15000, maximumAge:30000 }
      );
    });

    document.body.appendChild(btn);
  }

  /* ── REMOVED: The old mobile-only setTimeout block that called
     showLocationButton after 4s — replaced by the 10s fallback
     inside detectLiveLocation() which works for all devices ── */

  let startAbort=null,endAbort=null;
  async function fetchSuggestions(q,ctrl){
    const params=new URLSearchParams({format:"json",q,limit:"8",addressdetails:"1",namedetails:"1",countrycodes:"in","accept-language":"en"});
    if(state.start?.lat){params.set("viewbox",`${state.start.lng-0.5},${state.start.lat+0.5},${state.start.lng+0.5},${state.start.lat-0.5}`);}
    const res=await fetch("https://nominatim.openstreetmap.org/search?"+params,{signal:ctrl.signal,headers:{"Accept-Language":"en"}});
    const data=await res.json();
    return{results:data.map(item=>({display_name:item.display_name,name:(item.namedetails?.name||(item.display_name||"").split(",")[0].trim()),lat:parseFloat(item.lat),lng:parseFloat(item.lon),type:item.type,class:item.class}))};
  }

  function renderSuggestions(container,payload,onPick){
    container.innerHTML="";const results=payload?.results||[];if(!results.length){container.classList.remove("suggest--open");return;}
    if(payload?.message){const m=document.createElement("div");m.className="suggest__msg";m.textContent=payload.message;container.appendChild(m);}
    results.forEach(item=>{const row=document.createElement("button");row.type="button";row.className="suggest__item";row.innerHTML=`<div class="suggest__name">${item.name||(item.display_name||"").split(",")[0]}</div><div class="suggest__meta">${item.display_name||""}</div>`;row.addEventListener("click",()=>onPick(item));container.appendChild(row);});
    container.classList.add("suggest--open");
  }

  const debouncedStartSuggest=debounce(async()=>{const q=ui.inputStart.value.trim();if(q.length<2){renderSuggestions(ui.sugStart,{results:[]},()=>{});return;}if(startAbort)startAbort.abort();startAbort=new AbortController();try{const p=await fetchSuggestions(q,startAbort);renderSuggestions(ui.sugStart,p,item=>{ui.inputStart.value=item.display_name;state.start={lat:item.lat,lng:item.lng,label:item.display_name};setStartMarker([item.lat,item.lng],"Start");renderSuggestions(ui.sugStart,{results:[]},()=>{});map.flyTo({center:[item.lng,item.lat],zoom:Math.max(14,map.getZoom())});fitToStartEnd();const btn=$("btn-locate-me");if(btn)btn.remove();});}catch(_){}},300);
  const debouncedEndSuggest=debounce(async()=>{const q=ui.inputEnd.value.trim();if(q.length<2){renderSuggestions(ui.sugEnd,{results:[]},()=>{});return;}if(endAbort)endAbort.abort();endAbort=new AbortController();try{const p=await fetchSuggestions(q,endAbort);renderSuggestions(ui.sugEnd,p,item=>{ui.inputEnd.value=item.display_name;state.end={lat:item.lat,lng:item.lng,label:item.display_name};setEndMarker([item.lat,item.lng],"Destination");renderSuggestions(ui.sugEnd,{results:[]},()=>{});map.flyTo({center:[item.lng,item.lat],zoom:Math.max(14,map.getZoom())});fitToStartEnd();});}catch(_){}},300);

  document.addEventListener("click",e=>{
    if(!e.target.closest?.("#start-wrap")&&!e.target.closest?.("#dest-wrap")&&
       !e.target.closest?.("#mob-start-wrap")&&!e.target.closest?.("#mob-dest-wrap")){
      ui.sugStart.classList.remove("suggest--open");
      ui.sugEnd.classList.remove("suggest--open");
    }
  });

  function attachInputListeners(){
    const inpS=ui.inputStart, inpE=ui.inputEnd;
    inpS.addEventListener("input",debouncedStartSuggest);
    inpE.addEventListener("input",debouncedEndSuggest);
    inpS.addEventListener("input",()=>{if(state.start&&inpS.value.trim()!==(state.start.label||"").trim()){state.start=null;startMarker?.remove();startMarker=null;}});
    inpE.addEventListener("input",()=>{if(state.end&&inpE.value.trim()!==(state.end.label||"").trim()){state.end=null;endMarker?.remove();endMarker=null;}});
    if(isMobile()){
      const ds=$("input-current"),de=$("input-dest");
      if(ds&&ds!==inpS){ds.addEventListener("input",debouncedStartSuggest);}
      if(de&&de!==inpE){de.addEventListener("input",debouncedEndSuggest);}
    }
  }
  attachInputListeners();

  function buildWeights(){const on=id=>$(id)&&$(id).checked;return{street_lighting:on("t-light")?0.25:0.05,crowd_density:on("t-crowd")?0.15:0.05,police_proximity:on("t-police")?0.10:0.03,cctv_coverage:on("t-cctv")?0.10:0.03,road_visibility:0.10,traffic_density:0.10,crime_rate:on("t-crime")?0.15:0.08,incident_reports:0.05};}

  function buildLabeledRoutes(routes){
    if(!routes?.length)return[];
    if(routes[0]?.route_label)return routes.map(r=>({key:r.route_label,route:r}));
    const s=[...routes].sort((a,b)=>b.route_score-a.route_score)[0];
    const f=[...routes].sort((a,b)=>a.duration_s-b.duration_s)[0];
    if(routes.length===1)return[{key:"Route",route:routes[0]}];
    if(routes.length===2)return[{key:"Safest Route",route:s},{key:"Fastest Route",route:f}];
    const b=routes.find(r=>r!==s&&r!==f)||routes[1];
    return[{key:"Safest Route",route:s},{key:"Balanced Route",route:b},{key:"Fastest Route",route:f}];
  }

  async function geocodeAddress(q){
    try{
      const params=new URLSearchParams({format:"json",q,limit:"1",addressdetails:"1",countrycodes:"in","accept-language":"en"});
      const res=await fetch("https://nominatim.openstreetmap.org/search?"+params,{headers:{"Accept-Language":"en"}});
      const data=await res.json();
      if(!data?.length)return null;
      return{lat:parseFloat(data[0].lat),lng:parseFloat(data[0].lon),display_name:data[0].display_name};
    }catch(_){return null;}
  }

  async function fetchRoutesAndScores(){
    const si=ui.inputStart.value.trim(),ei=ui.inputEnd.value.trim();
    if(!state.start&&si.length>=2){const g=await geocodeAddress(si);if(g){state.start={lat:g.lat,lng:g.lng,label:g.display_name||si};setStartMarker([g.lat,g.lng],"Start");}}
    if(!state.end&&ei.length>=2){const g=await geocodeAddress(ei);if(g){state.end={lat:g.lat,lng:g.lng,label:g.display_name||ei};setEndMarker([g.lat,g.lng],"Destination");}}
    if(!state.start||!state.end){setStatus("Select start and destination.");return;}
    setStatus("Finding routes and scoring safety…");clearRoutes();
    let res;try{res=await fetch("/api/routes",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({start:{lat:state.start.lat,lng:state.start.lng},end:{lat:state.end.lat,lng:state.end.lng},weights:buildWeights(),max_distance_m:280})});}catch(e){setStatus("Network error.");return;}
    let data=null;try{data=await res.json();}catch(_){}
    if(!res.ok||!data?.routes?.length){setStatus((data?.error||"No routes found")+". Try again.");return;}
    state.routes=data.routes;state.aiBest=data.ai_recommendation||null;
    const labeled=buildLabeledRoutes(data.routes);
    renderRouteCards(labeled);drawRoutes(labeled);state.selectedRouteKey=labeled[0].key;focusRoute(labeled[0]);fitToStartEnd();
    setStatus("✅ "+labeled.length+" routes — "+labeled.map(l=>l.key+" ("+Math.round(l.route.route_score)+"%)").join(" · "));
  }

  function renderRouteCards(labeled){
    ui.cards.innerHTML="";
    labeled.forEach(item=>{
      const r=item.route,pct=safetyPct(r.route_score),zC=zoneColor(r.zone),rC=routeColor(item.key);
      const card=document.createElement("div");
      card.className="card card--route"+(item.key===state.selectedRouteKey?" card--selected":"");
      card.dataset.routeKey=item.key;card.style.setProperty("--route-color",rC);card.style.cursor="pointer";
      const modes=r.duration_by_mode_s||{};
      card.innerHTML=`<div class="card__head"><div class="card__title">${routeIcon(item.key)} ${item.key}</div><div class="chip-badge" style="border-color:${zC};color:${zC}">${r.zone.toUpperCase()}</div></div><div class="bar"><div class="bar__fill" style="width:${pct}%;background:${rC}"></div></div><div class="card__meta"><span class="pill">📍 ${fmtKm(r.distance_m)}</span><span class="pill">⏱ ${fmtMin(r.duration_s)}</span><span class="pill pill--accent" style="color:${zC}">🛡 ${pct}%</span></div><div class="card__meta"><span class="pill">🚗 ${fmtMin(modes.car||r.duration_s)}</span><span class="pill">🚲 ${fmtMin(modes.bike||r.duration_s)}</span><span class="pill">🚶 ${fmtMin(modes.walk||r.duration_s)}</span></div>`;
      card.addEventListener("click",()=>{state.selectedRouteKey=item.key;[...ui.cards.querySelectorAll(".card--route")].forEach(c=>c.classList.remove("card--selected"));card.classList.add("card--selected");focusRoute(item);drawRoutes(labeled);});
      ui.cards.appendChild(card);
    });
  }

  function drawRoutes(labeled){
    clearRoutes();
    [...labeled].sort((a,b)=>(a.key===state.selectedRouteKey?1:0)-(b.key===state.selectedRouteKey?1:0)).forEach((item,idx)=>{
      const r=item.route,color=routeColor(item.key),isSel=item.key===state.selectedRouteKey;
      if(!r.geometry?.coordinates)return;
      const sid="route-"+idx,lid="route-layer-"+idx;
      map.addSource(sid,{type:"geojson",data:{type:"Feature",properties:{},geometry:r.geometry}});
      map.addLayer({id:lid,type:"line",source:sid,layout:{"line-join":"round","line-cap":"round"},paint:{"line-color":color,"line-width":isSel?7:3,"line-opacity":isSel?0.95:0.45}});
      routeSrcIds.push(sid);routeLyrIds.push(lid);
    });
    const sel=labeled.find(x=>x.key===state.selectedRouteKey)||labeled[0];
    if(sel?.route.geometry?.coordinates){
      if(map.getSource("route-arrows"))map.removeSource("route-arrows");if(map.getLayer(arrowLyr))map.removeLayer(arrowLyr);
      map.addSource("route-arrows",{type:"geojson",data:{type:"Feature",properties:{},geometry:sel.route.geometry}});
      map.addLayer({id:arrowLyr,type:"line",source:"route-arrows",layout:{"line-join":"round","line-cap":"round"},paint:{"line-color":"#e8efff","line-width":1.5,"line-opacity":0.3,"line-dasharray":[2,2]}});
      routeLyrIds.push(arrowLyr);
    }
  }

  function updateTraveledOverlay(traveledIdx){
    const nav=state.nav;if(!nav.active||!nav.coordsLatLng.length)return;
    const traveledCoords=nav.coordsLatLng.slice(0,traveledIdx+1).map(c=>[c[1],c[0]]);
    if(traveledCoords.length<2)return;
    const geom={type:"LineString",coordinates:traveledCoords};
    if(map.getSource("route-traveled")){map.getSource("route-traveled").setData({type:"Feature",properties:{},geometry:geom});}
    else{map.addSource("route-traveled",{type:"geojson",data:{type:"Feature",properties:{},geometry:geom}});map.addLayer({id:"route-traveled",type:"line",source:"route-traveled",layout:{"line-join":"round","line-cap":"round"},paint:{"line-color":"rgba(255,255,255,0.25)","line-width":7}});}
  }

  function renderIncidents(routeObj){
    const worst=(routeObj.worst_points||[]).slice(0,8);
    if(!worst.length){ui.incidents.innerHTML='<div class="incident incident--muted">No nearby high‑risk points detected.</div>';return;}
    ui.incidents.innerHTML="";
    worst.forEach(p=>{const sp=typeof p.safety_percent==="number"?Math.round(p.safety_percent):null;const div=document.createElement("div");div.className="incident";div.innerHTML=`<div class="incident__row"><div class="incident__title">${p.area||"Point #"+p.id}</div><div class="incident__score">${sp===null?"—":sp+"%"}</div></div><div class="incident__sub">Distance: ${p.distance_to_route_m} m • Zone: ${p.zone}</div>`;div.addEventListener("click",()=>map.flyTo({center:[p.lng,p.lat],zoom:16}));ui.incidents.appendChild(div);});
  }

  function focusRoute(item){
    const r=item.route;
    ui.bnReco.textContent=(r.ai_message||item.key).slice(0,80);
    const now=new Date(),arrive=new Date(now.getTime()+(r.duration_s||0)*1000);
    const h=arrive.getHours(),m=arrive.getMinutes();
    ui.bnEta.textContent=(h%12||12)+":"+String(m).padStart(2,"0")+(h>=12?" PM":" AM");
    ui.bnRemaining.textContent=fmtKm(r.distance_m)+" • "+fmtMin(r.duration_s||0);
    ui.bnNext.textContent=extractNextInstruction(r)||"—";
    ui.bnScore.textContent=r.route_score+"% ("+(r.zone||"—").toUpperCase()+")";
    ui.bnCurrent.textContent=state.start?(state.start.label||state.start.lat.toFixed(4)+", "+state.start.lng.toFixed(4)):"—";
    ui.bnDest.textContent=state.end?(state.end.label||state.end.lat.toFixed(4)+", "+state.end.lng.toFixed(4)):"—";
    renderIncidents(r);
  }

  $("btn-find").addEventListener("click",fetchRoutesAndScores);
  $("btn-reset").addEventListener("click",()=>{
    state.start=state.end=null;state.routes=[];state.selectedRouteKey="Route";
    stopNavigation();clearRoutes();startMarker?.remove();startMarker=null;endMarker?.remove();endMarker=null;
    ui.inputStart.value=ui.inputEnd.value="";
    ["bnCurrent","bnDest","bnReco","bnEta","bnRemaining","bnSpeed","bnNext","bnScore"].forEach(k=>{if(ui[k])ui[k].textContent="—";});
    setStatus("Reset. Search for start and destination.");
  });
  $("btn-heat").addEventListener("click",()=>{state.heatEnabled=!state.heatEnabled;if(map.getLayer(hmLyr))map.setPaintProperty(hmLyr,"heatmap-opacity",state.heatEnabled?0.7:0);});
  if($("btn-report"))$("btn-report").addEventListener("click",submitReport);
  if($("btn-report-submit"))$("btn-report-submit").addEventListener("click",submitReport);
  $("btn-start-nav").addEventListener("click",()=>{if(state.navigating){stopNavigation();return;}startNavigation();});
  if($("nav-stop-btn"))$("nav-stop-btn").addEventListener("click",stopNavigation);
  if($("arrived-dismiss"))$("arrived-dismiss").addEventListener("click",()=>{if(ui.arrivedOverlay)ui.arrivedOverlay.classList.remove("active");stopNavigation();});

  async function submitReport(){
    if(!state.lastClick){setStatus("Click the map to choose a report location first.");return;}
    const res=await fetch("/api/report",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({lat:state.lastClick.lat,lng:state.lastClick.lng,place_name:($("report-place")?.value||"").trim(),description:$("report-desc")?.value||"",rating:$("report-rating")?.value?parseInt($("report-rating").value):null})});
    const data=await res.json();setStatus(data?.ok?"Report submitted.":"Report failed: "+(data?.error||"unknown"));
  }

  function extractStepInstructions(routeObj){
    const steps=[];let distAcc=0;
    for(const leg of(routeObj.legs||[])){
      for(const st of(leg.steps||[])){
        distAcc+=(st.distance||0);
        const m=st.maneuver||{};const type=(m.type||"").toLowerCase();
        const instr=(st.instruction||st.ref||"").trim()||[m.type,m.modifier,st.name].filter(Boolean).join(" ")||"Continue";
        let arrow="⬆️";
        if(type.includes("left"))arrow="⬅️";
        else if(type.includes("right"))arrow="➡️";
        else if(type.includes("uturn"))arrow="🔄";
        else if(type.includes("arrive"))arrow="🏁";
        else if(type.includes("roundabout"))arrow="🔃";
        steps.push({instruction:instr,distanceFromStart:distAcc,arrow});
      }
    }
    return steps;
  }
  function extractNextInstruction(routeObj){const s=extractStepInstructions(routeObj);return s.length?s[0].instruction:"Continue";}
  function getStepForPosition(routeObj,traveledM){
    const steps=extractStepInstructions(routeObj);
    if(!steps.length)return{instruction:"Head toward destination",arrow:"⬆️",distToNext:0};
    for(let i=0;i<steps.length;i++){if(steps[i].distanceFromStart>traveledM+10){return{instruction:steps[i].instruction,arrow:steps[i].arrow,distToNext:steps[i].distanceFromStart-traveledM};}}
    return{instruction:"Arrive at destination",arrow:"🏁",distToNext:0};
  }

  function pickSelectedRoute(){const labeled=buildLabeledRoutes(state.routes);if(!labeled.length)return null;return labeled.find(l=>l.key===state.selectedRouteKey)||labeled[0];}

  function startNavigation(){
    const picked=pickSelectedRoute();if(!picked?.route){setStatus("Generate routes first.");return;}
    const coords=(picked.route.geometry?.coordinates||[]);
    const coordsLatLng=coords.map(c=>[c[1],c[0]]);
    state.nav.active=picked;state.nav.coordsLatLng=coordsLatLng;state.nav.cumDistM=buildCumDist(coordsLatLng);
    state.nav.startedAtMs=Date.now();state.nav.lastRerouteAtMs=0;state.nav.traveledIdx=0;state.nav.arrivedFired=false;
    state.navigating=true;
    if(ui.navBanner)ui.navBanner.classList.add("active");
    if(ui.progressBar){ui.progressBar.style.display="block";ui.progressBar.style.width="0%";}
    $("btn-start-nav").textContent="Stop Navigation";
    if(coordsLatLng.length>0)map.easeTo({center:[coordsLatLng[0][1],coordsLatLng[0][0]],zoom:16,duration:800});
    ui.bnDest.textContent=state.end?(state.end.label||"Destination"):"—";
    setStatus("🧭 Navigation started — "+picked.key);
  }

  function stopNavigation(){
    state.navigating=false;state.nav.active=null;state.nav.coordsLatLng=[];state.nav.cumDistM=[];
    state.nav.startedAtMs=null;state.nav.lastRerouteAtMs=0;state.nav.traveledIdx=0;state.nav.arrivedFired=false;
    lastNavPos=lastNavTime=null;
    if(ui.navBanner)ui.navBanner.classList.remove("active");
    if(ui.progressBar){ui.progressBar.style.display="none";ui.progressBar.style.width="0%";}
    $("btn-start-nav").textContent="Start Navigation";
    if(navDot){navDot.remove();navDot=null;}
    if(map.getLayer("route-traveled"))map.removeLayer("route-traveled");if(map.getSource("route-traveled"))map.removeSource("route-traveled");
    if(ui.bnNext)ui.bnNext.textContent="—";if(ui.bnSpeed)ui.bnSpeed.textContent="—";
    setStatus("Navigation stopped.");
  }

  function showArrived(){
    if(state.nav.arrivedFired)return;state.nav.arrivedFired=true;
    const dest=state.end?.label||"your destination";
    if(ui.arrivedOverlay){if(ui.arrivedDest)ui.arrivedDest.textContent="You have reached "+dest;ui.arrivedOverlay.classList.add("active");}
    if(ui.progressBar)ui.progressBar.style.width="100%";
  }

  function updateNavigation(lat,lng,gpsSpeed){
    const nav=state.nav,active=nav.active;
    if(!active?.route||!nav.coordsLatLng.length)return;
    if(!navDot){navDot=new maplibregl.Marker({element:createNavDotEl(),anchor:"center"}).setLngLat([lng,lat]).addTo(map);}
    else{navDot.setLngLat([lng,lat]);}
    const near=nearestIndex(nav.coordsLatLng,lat,lng);
    nav.traveledIdx=Math.max(nav.traveledIdx,near.idx);
    const distToRoute=near.distM;
    const total=nav.cumDistM[nav.cumDistM.length-1]||active.route.distance_m||0;
    const traveled=nav.cumDistM[nav.traveledIdx]||0;
    const remaining=Math.max(0,total-traveled);
    if(remaining<30||(state.end&&haversineM(lat,lng,state.end.lat,state.end.lng)<40)){showArrived();return;}
    const pct=total>0?Math.min(99,Math.round((traveled/total)*100)):0;
    if(ui.progressBar)ui.progressBar.style.width=pct+"%";
    updateTraveledOverlay(nav.traveledIdx);
    map.easeTo({center:[lng,lat],zoom:16,duration:800,essential:true});
    const step=getStepForPosition(active.route,traveled);
    if(ui.navArrow)ui.navArrow.textContent=step.arrow;
    if(ui.navInstruction)ui.navInstruction.textContent=step.instruction;
    if(ui.navDistNext)ui.navDistNext.textContent=step.distToNext>0?(step.distToNext>=1000?fmtKm(step.distToNext):Math.round(step.distToNext)+"m"):"";
    const remStr=remaining>=1000?fmtKm(remaining):Math.round(remaining)+" m";
    if(ui.navRemaining)ui.navRemaining.textContent=remStr;ui.bnRemaining.textContent=remStr;
    const durTotal=active.route.duration_s||0;
    const etaS=durTotal>0&&total>0?(durTotal*(remaining/total)):durTotal;
    const now=new Date(),arrive=new Date(now.getTime()+etaS*1000);
    const h=arrive.getHours(),m=arrive.getMinutes();
    const etaStr=(h%12||12)+":"+String(m).padStart(2,"0")+(h>=12?" PM":" AM");
    if(ui.navEta)ui.navEta.textContent=etaStr;ui.bnEta.textContent=etaStr;
    let speedStr="—";
    if(gpsSpeed!=null&&gpsSpeed>=0){speedStr=Math.round(gpsSpeed*3.6)+" km/h";}
    else{const t=Date.now();if(lastNavPos&&lastNavTime&&(t-lastNavTime)>=1500){const dt=(t-lastNavTime)/1000,d=haversineM(lastNavPos[0],lastNavPos[1],lat,lng),spd=d/dt*3.6;speedStr=spd<0.5?"0 km/h":spd.toFixed(0)+" km/h";lastNavPos=[lat,lng];lastNavTime=t;}else if(!lastNavPos){lastNavPos=[lat,lng];lastNavTime=Date.now();}}
    if(ui.navSpeed)ui.navSpeed.textContent=speedStr;ui.bnSpeed.textContent=speedStr;
    if(ui.navSafety)ui.navSafety.textContent=active.route.route_score+"%";
    ui.bnScore.textContent=active.route.route_score+"% ("+(active.route.zone||"—").toUpperCase()+")";
    ui.bnNext.textContent=step.instruction;
    ui.bnReco.textContent=(active.route.ai_message||"").slice(0,80);
    ui.bnCurrent.textContent=lat.toFixed(4)+", "+lng.toFixed(4);
    if(distToRoute>80)rerouteFrom(lat,lng);
  }

  async function rerouteFrom(lat,lng){
    const now=Date.now();if(now-state.nav.lastRerouteAtMs<8000)return;state.nav.lastRerouteAtMs=now;if(!state.end)return;
    setStatus("Off route — recalculating…");state.start={lat,lng,label:"Current Location"};ui.inputStart.value="Current Location";setStartMarker([lat,lng],"Start");
    const prevKey=state.selectedRouteKey;await fetchRoutesAndScores();
    const labeled=buildLabeledRoutes(state.routes);const sel=labeled.find(l=>l.key===prevKey)||labeled[0];
    state.selectedRouteKey=sel.key;[...ui.cards.querySelectorAll(".card--route")].forEach(c=>c.classList.toggle("card--selected",(c.dataset.routeKey||"")===sel.key));
    focusRoute(sel);drawRoutes(labeled);startNavigation();
  }
})();