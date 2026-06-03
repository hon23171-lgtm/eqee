import * as THREE from "three";

// ----------------------------------------------------------------------------
// Block types & pixel textures
// ----------------------------------------------------------------------------
function makeTexture(draw) {
  const c = document.createElement("canvas");
  c.width = c.height = 16;
  const ctx = c.getContext("2d");
  draw(ctx);
  const tex = new THREE.CanvasTexture(c);
  tex.magFilter = THREE.NearestFilter;
  tex.minFilter = THREE.NearestFilter;
  return tex;
}

function noisyFill(ctx, base, spots) {
  ctx.fillStyle = base;
  ctx.fillRect(0, 0, 16, 16);
  for (let i = 0; i < 40; i++) {
    ctx.fillStyle = spots[(Math.random() * spots.length) | 0];
    ctx.fillRect((Math.random() * 16) | 0, (Math.random() * 16) | 0, 1, 1);
  }
}

const TEX = {
  grassTop: makeTexture((c) => noisyFill(c, "#5d9c3a", ["#6cad44", "#4f8a2f", "#74b84d"])),
  grassSide: makeTexture((c) => {
    noisyFill(c, "#79553a", ["#8a6242", "#6a4a32"]);
    c.fillStyle = "#5d9c3a";
    c.fillRect(0, 0, 16, 4);
    for (let i = 0; i < 16; i++) {
      c.fillStyle = Math.random() > 0.5 ? "#4f8a2f" : "#6cad44";
      c.fillRect(i, 3 + ((Math.random() * 2) | 0), 1, 1);
    }
  }),
  dirt: makeTexture((c) => noisyFill(c, "#79553a", ["#8a6242", "#6a4a32", "#5e4128"])),
  stone: makeTexture((c) => noisyFill(c, "#7f7f7f", ["#8e8e8e", "#6e6e6e", "#999"])),
  planks: makeTexture((c) => {
    noisyFill(c, "#9c6b3f", ["#a9753f", "#8a5d34"]);
    c.fillStyle = "#6a4a28";
    for (let y = 0; y < 16; y += 4) c.fillRect(0, y, 16, 1);
  }),
  log: makeTexture((c) => {
    noisyFill(c, "#6e4a2a", ["#7d5430", "#5e3f24"]);
    c.fillStyle = "#5e3f24";
    for (let x = 0; x < 16; x += 5) c.fillRect(x, 0, 1, 16);
  }),
  leaves: makeTexture((c) => noisyFill(c, "#3f7d2f", ["#4f8a2f", "#356b27", "#5aa03a"])),
};

function sideMat(tex) {
  return new THREE.MeshLambertMaterial({ map: tex });
}

// order: +x, -x, +y(top), -y(bottom), +z, -z
const BLOCKS = {
  grass: {
    name: "Трава",
    color: "#5d9c3a",
    mats: [TEX.grassSide, TEX.grassSide, TEX.grassTop, TEX.dirt, TEX.grassSide, TEX.grassSide].map(sideMat),
  },
  dirt: { name: "Земля", color: "#79553a", mats: Array(6).fill(sideMat(TEX.dirt)) },
  stone: { name: "Камень", color: "#7f7f7f", mats: Array(6).fill(sideMat(TEX.stone)) },
  planks: { name: "Доски", color: "#9c6b3f", mats: Array(6).fill(sideMat(TEX.planks)) },
  log: {
    name: "Бревно",
    color: "#6e4a2a",
    mats: [TEX.log, TEX.log, TEX.dirt, TEX.dirt, TEX.log, TEX.log].map(sideMat),
  },
  leaves: { name: "Листва", color: "#3f7d2f", mats: Array(6).fill(sideMat(TEX.leaves)) },
};

const HOTBAR = ["grass", "dirt", "stone", "planks", "log", "leaves"];

// ----------------------------------------------------------------------------
// Scene setup
// ----------------------------------------------------------------------------
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x87ceeb);
scene.fog = new THREE.Fog(0x87ceeb, 25, 60);

const camera = new THREE.PerspectiveCamera(75, innerWidth / innerHeight, 0.1, 1000);

const renderer = new THREE.WebGLRenderer({ antialias: false });
renderer.setSize(innerWidth, innerHeight);
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
document.body.appendChild(renderer.domElement);

scene.add(new THREE.AmbientLight(0xffffff, 0.75));
const sun = new THREE.DirectionalLight(0xffffff, 0.7);
sun.position.set(20, 40, 10);
scene.add(sun);

const boxGeo = new THREE.BoxGeometry(1, 1, 1);

// ----------------------------------------------------------------------------
// World — blocks stored in a Map keyed "x,y,z"
// ----------------------------------------------------------------------------
const world = new Map();
const blockMeshes = []; // for raycasting

const key = (x, y, z) => `${x},${y},${z}`;

function addBlock(x, y, z, type) {
  const k = key(x, y, z);
  if (world.has(k)) return;
  const mesh = new THREE.Mesh(boxGeo, BLOCKS[type].mats);
  mesh.position.set(x, y, z);
  mesh.userData = { x, y, z, type, kind: "block" };
  scene.add(mesh);
  world.set(k, mesh);
  blockMeshes.push(mesh);
}

function removeBlock(mesh) {
  const { x, y, z } = mesh.userData;
  world.delete(key(x, y, z));
  scene.remove(mesh);
  const i = blockMeshes.indexOf(mesh);
  if (i >= 0) blockMeshes.splice(i, 1);
}

function solidAt(x, y, z) {
  return world.has(key(x, y, z));
}

// Simple terrain: gentle hills + dirt/stone layers, a few trees.
const SIZE = 16; // -SIZE..SIZE
function heightAt(x, z) {
  return Math.round(
    1.6 * Math.sin(x * 0.3) * Math.cos(z * 0.25) + Math.sin(x * 0.13 + z * 0.17)
  );
}

for (let x = -SIZE; x <= SIZE; x++) {
  for (let z = -SIZE; z <= SIZE; z++) {
    const h = heightAt(x, z);
    addBlock(x, h, z, "grass");
    addBlock(x, h - 1, z, "dirt");
    addBlock(x, h - 2, z, "dirt");
    addBlock(x, h - 3, z, "stone");
  }
}

// scatter a few trees
for (let t = 0; t < 8; t++) {
  const x = ((Math.random() * SIZE * 2) | 0) - SIZE;
  const z = ((Math.random() * SIZE * 2) | 0) - SIZE;
  const h = heightAt(x, z);
  for (let i = 1; i <= 4; i++) addBlock(x, h + i, z, "log");
  for (let dx = -2; dx <= 2; dx++)
    for (let dz = -2; dz <= 2; dz++)
      for (let dy = 4; dy <= 6; dy++) {
        if (Math.abs(dx) + Math.abs(dz) + Math.abs(dy - 5) > 3) continue;
        if (dx === 0 && dz === 0 && dy <= 4) continue;
        addBlock(x + dx, h + dy, z + dz, "leaves");
      }
}

// ----------------------------------------------------------------------------
// Pigs 🐷
// ----------------------------------------------------------------------------
const pigs = [];
const pigMeshes = []; // raycast targets

function makePig(x, z) {
  const group = new THREE.Group();
  const skin = new THREE.MeshLambertMaterial({ color: 0xf0a0a8 });
  const snoutMat = new THREE.MeshLambertMaterial({ color: 0xe07d88 });

  const body = new THREE.Mesh(new THREE.BoxGeometry(0.9, 0.7, 1.3), skin);
  body.position.y = 0.55;
  group.add(body);

  const head = new THREE.Mesh(new THREE.BoxGeometry(0.65, 0.6, 0.55), skin);
  head.position.set(0, 0.65, 0.85);
  group.add(head);

  const snout = new THREE.Mesh(new THREE.BoxGeometry(0.3, 0.25, 0.1), snoutMat);
  snout.position.set(0, 0.6, 1.16);
  group.add(snout);

  const legGeo = new THREE.BoxGeometry(0.22, 0.4, 0.22);
  for (const [lx, lz] of [[-0.28, 0.45], [0.28, 0.45], [-0.28, -0.45], [0.28, -0.45]]) {
    const leg = new THREE.Mesh(legGeo, skin);
    leg.position.set(lx, 0.2, lz);
    group.add(leg);
  }

  group.position.set(x, 0, z);
  group.userData = {
    kind: "pig",
    hp: 3,
    dir: Math.random() * Math.PI * 2,
    timer: 0,
    hurt: 0,
    mats: [skin, snoutMat],
  };
  // mark child meshes so raycaster can resolve to the group
  group.traverse((o) => {
    if (o.isMesh) {
      o.userData.pig = group;
      pigMeshes.push(o);
    }
  });
  scene.add(group);
  pigs.push(group);
  return group;
}

function groundHeight(x, z) {
  const gx = Math.round(x), gz = Math.round(z);
  for (let y = 12; y > -8; y--) if (solidAt(gx, y, gz)) return y + 0.5;
  return 0.5;
}

for (let i = 0; i < 6; i++) {
  const x = ((Math.random() * SIZE * 2) | 0) - SIZE;
  const z = ((Math.random() * SIZE * 2) | 0) - SIZE;
  makePig(x, z);
}

function damagePig(pig) {
  const ud = pig.userData;
  ud.hp -= 1;
  ud.hurt = 0.25;
  ud.mats.forEach((m) => m.color.set(0xff5555));
  // knockback away from player
  const away = new THREE.Vector3().subVectors(pig.position, player.pos).setY(0).normalize();
  pig.position.addScaledVector(away, 0.6);
  if (ud.hp <= 0) {
    scene.remove(pig);
    pigs.splice(pigs.indexOf(pig), 1);
    pig.traverse((o) => {
      const i = pigMeshes.indexOf(o);
      if (i >= 0) pigMeshes.splice(i, 1);
    });
  }
}

function updatePigs(dt) {
  for (const pig of pigs) {
    const ud = pig.userData;
    ud.timer -= dt;
    if (ud.timer <= 0) {
      ud.dir = Math.random() * Math.PI * 2;
      ud.timer = 1.5 + Math.random() * 2.5;
    }
    if (ud.hurt > 0) {
      ud.hurt -= dt;
      if (ud.hurt <= 0) ud.mats.forEach((m, i) => m.color.set(i ? 0xe07d88 : 0xf0a0a8));
    }
    const speed = 1.1;
    const nx = pig.position.x + Math.sin(ud.dir) * speed * dt;
    const nz = pig.position.z + Math.cos(ud.dir) * speed * dt;
    if (Math.abs(nx) < SIZE && Math.abs(nz) < SIZE) {
      pig.position.x = nx;
      pig.position.z = nz;
      pig.rotation.y = ud.dir;
    } else {
      ud.timer = 0;
    }
    pig.position.y = groundHeight(pig.position.x, pig.position.z) - 0.5;
  }
}

// ----------------------------------------------------------------------------
// Player + physics
// ----------------------------------------------------------------------------
const player = {
  pos: new THREE.Vector3(0.5, heightAt(0, 0) + 2, 0.5),
  vel: new THREE.Vector3(),
  onGround: false,
  yaw: 0,
  pitch: 0,
  hp: 10,
  width: 0.6,
  height: 1.8,
  eye: 1.62,
};

function collides(px, py, pz) {
  const hw = player.width / 2;
  const minX = px - hw, maxX = px + hw;
  const minY = py, maxY = py + player.height;
  const minZ = pz - hw, maxZ = pz + hw;
  for (let x = Math.round(minX) - 1; x <= Math.round(maxX) + 1; x++)
    for (let y = Math.round(minY) - 1; y <= Math.round(maxY) + 1; y++)
      for (let z = Math.round(minZ) - 1; z <= Math.round(maxZ) + 1; z++) {
        if (!solidAt(x, y, z)) continue;
        if (
          maxX > x - 0.5 && minX < x + 0.5 &&
          maxY > y - 0.5 && minY < y + 0.5 &&
          maxZ > z - 0.5 && minZ < z + 0.5
        ) return true;
      }
  return false;
}

function movePlayer(dt) {
  const speed = keys.has("ShiftLeft") ? 2.6 : 5.2;
  const forward = new THREE.Vector3(-Math.sin(player.yaw), 0, -Math.cos(player.yaw));
  const right = new THREE.Vector3(Math.cos(player.yaw), 0, -Math.sin(player.yaw));
  const wish = new THREE.Vector3();
  if (keys.has("KeyW")) wish.add(forward);
  if (keys.has("KeyS")) wish.sub(forward);
  if (keys.has("KeyD")) wish.add(right);
  if (keys.has("KeyA")) wish.sub(right);
  if (wish.lengthSq() > 0) wish.normalize().multiplyScalar(speed);

  player.vel.x = wish.x;
  player.vel.z = wish.z;
  player.vel.y -= 25 * dt; // gravity

  // X axis
  let nx = player.pos.x + player.vel.x * dt;
  if (!collides(nx, player.pos.y, player.pos.z)) player.pos.x = nx;
  else player.vel.x = 0;
  // Z axis
  let nz = player.pos.z + player.vel.z * dt;
  if (!collides(player.pos.x, player.pos.y, nz)) player.pos.z = nz;
  else player.vel.z = 0;
  // Y axis
  let ny = player.pos.y + player.vel.y * dt;
  if (!collides(player.pos.x, ny, player.pos.z)) {
    player.pos.y = ny;
    player.onGround = false;
  } else {
    if (player.vel.y < 0) player.onGround = true;
    player.vel.y = 0;
  }

  if (player.pos.y < -20) {
    player.pos.set(0.5, heightAt(0, 0) + 2, 0.5);
    player.vel.set(0, 0, 0);
  }

  camera.position.set(player.pos.x, player.pos.y + player.eye, player.pos.z);
  camera.rotation.set(player.pitch, player.yaw, 0, "YXZ");
}

// ----------------------------------------------------------------------------
// Input
// ----------------------------------------------------------------------------
const keys = new Set();
let selected = 0;

addEventListener("keydown", (e) => {
  keys.add(e.code);
  if (e.code === "Space" && player.onGround) {
    player.vel.y = 8.2;
    player.onGround = false;
  }
  if (e.code.startsWith("Digit")) {
    const n = parseInt(e.code.slice(5), 10) - 1;
    if (n >= 0 && n < HOTBAR.length) setSelected(n);
  }
});
addEventListener("keyup", (e) => keys.delete(e.code));

addEventListener("wheel", (e) => {
  if (document.pointerLockElement !== renderer.domElement) return;
  setSelected((selected + (e.deltaY > 0 ? 1 : -1) + HOTBAR.length) % HOTBAR.length);
});

addEventListener("mousemove", (e) => {
  if (document.pointerLockElement !== renderer.domElement) return;
  player.yaw -= e.movementX * 0.0025;
  player.pitch -= e.movementY * 0.0025;
  const lim = Math.PI / 2 - 0.01;
  player.pitch = Math.max(-lim, Math.min(lim, player.pitch));
});

const raycaster = new THREE.Raycaster();
raycaster.far = 7;
const screenCenter = new THREE.Vector2(0, 0);

addEventListener("mousedown", (e) => {
  if (document.pointerLockElement !== renderer.domElement) return;
  raycaster.setFromCamera(screenCenter, camera);

  // pigs first (only on left click)
  if (e.button === 0) {
    const pigHit = raycaster.intersectObjects(pigMeshes, false)[0];
    const blockHit = raycaster.intersectObjects(blockMeshes, false)[0];
    if (pigHit && (!blockHit || pigHit.distance < blockHit.distance)) {
      damagePig(pigHit.object.userData.pig);
      return;
    }
    if (blockHit) removeBlock(blockHit.object);
    return;
  }

  if (e.button === 2) {
    const blockHit = raycaster.intersectObjects(blockMeshes, false)[0];
    if (!blockHit) return;
    const n = blockHit.face.normal;
    const b = blockHit.object.userData;
    const nx = b.x + n.x, ny = b.y + n.y, nz = b.z + n.z;
    // don't place inside the player
    const hw = player.width / 2;
    const insideX = nx + 0.5 > player.pos.x - hw && nx - 0.5 < player.pos.x + hw;
    const insideZ = nz + 0.5 > player.pos.z - hw && nz - 0.5 < player.pos.z + hw;
    const insideY = ny + 0.5 > player.pos.y && ny - 0.5 < player.pos.y + player.height;
    if (insideX && insideY && insideZ) return;
    addBlock(nx, ny, nz, HOTBAR[selected]);
  }
});

addEventListener("contextmenu", (e) => e.preventDefault());

// ----------------------------------------------------------------------------
// UI: hotbar, health, overlay
// ----------------------------------------------------------------------------
const hotbarEl = document.getElementById("hotbar");
HOTBAR.forEach((type, i) => {
  const slot = document.createElement("div");
  slot.className = "slot";
  slot.innerHTML = `<span class="slot__num">${i + 1}</span><span class="slot__swatch" style="background:${BLOCKS[type].color}"></span>`;
  slot.title = BLOCKS[type].name;
  hotbarEl.appendChild(slot);
});

function setSelected(i) {
  selected = i;
  [...hotbarEl.children].forEach((s, idx) => s.classList.toggle("active", idx === i));
}
setSelected(0);

const healthEl = document.getElementById("health");
function renderHealth() {
  healthEl.textContent = "❤".repeat(player.hp) + "♡".repeat(Math.max(0, 10 - player.hp));
}
renderHealth();

const overlay = document.getElementById("overlay");
document.getElementById("startBtn").addEventListener("click", () => {
  renderer.domElement.requestPointerLock();
});
document.addEventListener("pointerlockchange", () => {
  overlay.classList.toggle("hidden", document.pointerLockElement === renderer.domElement);
});

addEventListener("resize", () => {
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
});

// ----------------------------------------------------------------------------
// Loop
// ----------------------------------------------------------------------------
let last = performance.now();
function tick(now) {
  const dt = Math.min((now - last) / 1000, 0.05);
  last = now;
  if (document.pointerLockElement === renderer.domElement) {
    movePlayer(dt);
    updatePigs(dt);
  }
  renderer.render(scene, camera);
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);
