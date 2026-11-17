function stubEl(){ var o = { innerHTML:"", textContent:"", value:"5", style:{}, dataset:{c:"yes"}, disabled:false, offsetWidth:1,
  classList:{add:function(){},remove:function(){},toggle:function(){}}, focus:function(){}, setAttribute:function(){},
  querySelectorAll:function(){ return [] }, querySelector:function(){ return stubEl() }, remove:function(){}, appendChild:function(){},
  getBoundingClientRect:function(){ return {left:0,top:0,width:100,height:100} }, animate:function(){ return {} } }; return o }
var els = {}; var document = { getElementById:function(id){ return els[id] || (els[id] = stubEl()) }, addEventListener:function(){},
  querySelectorAll:function(){ return [] }, createElement:function(){ return stubEl() }, createElementNS:function(){ return stubEl() } };
var matchMedia = function(){ return {matches:true} }; var window = {}; var localStorage = { getItem:function(){return null}, setItem:function(){} };
var pending = []; var setTimeout = function(f){ pending.push(f); return 0 }; var setInterval = function(){ return 0 }; var clearInterval = function(){};
function flush(){ var n=0; while (pending.length && n < 100000) { pending.shift()(); n++ } }
var performance = { now: function(){ return 0 } }; var requestAnimationFrame = function(){ return 0 };
