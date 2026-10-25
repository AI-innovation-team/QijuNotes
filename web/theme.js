// Small, CSP-compatible pre-paint restore. Keep the main application deferred.
(()=>{let theme='light';try{if(localStorage.getItem('dsh-theme')==='dark')theme='dark';}catch{}document.documentElement.dataset.theme=theme;const meta=document.querySelector('meta[name="theme-color"]');if(meta)meta.content=theme==='dark'?'#161d27':'#ffffff';})();
