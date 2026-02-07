<script>
  import { onMount } from 'svelte'
  import { Play, Pause, Volume2, Maximize, SkipBack, SkipForward, Type } from 'lucide-svelte'
  import Timeline from './Timeline.svelte'
  
  export let jobId // Prop passed from App.svelte

  let videoElement
  let isPlaying = false
  let currentTime = 0
  let duration = 0
  let volume = 1
  let videoUrl = '' // To be loaded from API
  
  let loading = true
  let error = null

  // Subtitle overlay state
  let subtitlePosition = { x: 50, y: 15 }
  let subtitleText = "Your subtitle text here"
  let isDragging = false
  let dragStart = { x: 0, y: 0 }
  let subtitleSize = { width: 300, height: 60 }
  let isResizing = false
  
  function togglePlay() {
    if (videoElement) {
      if (isPlaying) {
        videoElement.pause()
      } else {
        videoElement.play()
      }
      isPlaying = !isPlaying
    }
  }
  
  function handleTimeUpdate() {
    if (videoElement) {
      currentTime = videoElement.currentTime
      duration = videoElement.duration || 0
      
      // Update active caption based on current time
      captions = captions.map(c => ({
        ...c,
        active: currentTime >= c.start && currentTime <= c.end
      }))
      
      // Update subtitleText from active caption
      const activeCaption = captions.find(c => c.active)
      subtitleText = activeCaption ? activeCaption.text : ""
    }
  }
  
  function formatTime(seconds) {
    const mins = Math.floor(seconds / 60)
    const secs = Math.floor(seconds % 60)
    return `${mins}:${secs.toString().padStart(2, '0')}`
  }
  
  // Drag handlers for subtitle
  function handleSubtitleMouseDown(e) {
    if (e.target.classList.contains('resize-handle')) {
      isResizing = true
    } else {
      isDragging = true
    }
    dragStart = { 
      x: e.clientX - (subtitlePosition.x / 100 * e.target.parentElement.offsetWidth),
      y: e.clientY - ((100 - subtitlePosition.y) / 100 * e.target.parentElement.offsetHeight)
    }
    e.preventDefault()
  }
  
  function handleMouseMove(e) {
    if (!isDragging && !isResizing) return
    
    const container = videoElement?.parentElement
    if (!container) return
    
    if (isDragging) {
      const rect = container.getBoundingClientRect()
      const x = ((e.clientX - dragStart.x) / rect.width) * 100
      const y = 100 - ((e.clientY - dragStart.y) / rect.height) * 100
      
      subtitlePosition.x = Math.max(10, Math.min(90, x))
      subtitlePosition.y = Math.max(10, Math.min(90, y))
    }
  }
  
  function handleMouseUp() {
    isDragging = false
    isResizing = false
  }
  
  // Captions data
  let captions = []
  
  function srtTimeToSeconds(srtTime) {
    const [hours, minutes, secondsAndMillis] = srtTime.split(':')
    const [seconds, millis] = secondsAndMillis.split(',')
    return parseInt(hours, 10) * 3600 +
           parseInt(minutes, 10) * 60 +
           parseInt(seconds, 10) +
           parseInt(millis, 10) / 1000
  }

  function parseSrt(srtContent) {
    const lines = srtContent.split(/\r?\n/)
    let parsedCaptions = []
    let currentCaption = null
    let idCounter = 1

    for (const line of lines) {
      if (line.trim() === '') {
        if (currentCaption && currentCaption.text.trim() !== '') {
          parsedCaptions.push(currentCaption)
        }
        currentCaption = null
      } else if (currentCaption === null) {
        // This is the caption number, ignore for now, we use idCounter
        currentCaption = { id: idCounter++, text: '', active: false }
      } else if (line.includes('-->')) {
        const [startTime, endTime] = line.split('-->').map(s => s.trim())
        currentCaption.start = srtTimeToSeconds(startTime)
        currentCaption.end = srtTimeToSeconds(endTime)
      } else {
        currentCaption.text += (currentCaption.text === '' ? '' : '\n') + line.trim()
      }
    }
    // Add the last caption if it exists
    if (currentCaption && currentCaption.text.trim() !== '') {
      parsedCaptions.push(currentCaption)
    }
    return parsedCaptions
  }

  onMount(async () => {
    try {
      const response = await fetch(`/api/editor_data/${jobId}`)
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`)
      }
      const data = await response.json()
      if (data.status === 'success') {
        videoUrl = data.video_url
        captions = parseSrt(data.srt_content)
      } else {
        throw new Error(data.message || 'Failed to load editor data')
      }
    } catch (e) {
      console.error("Error fetching editor data:", e)
      error = e.message
    } finally {
      loading = false
    }
  })
  
  function handleCaptionUpdate(id, newText) {
    captions = captions.map(c => c.id === id ? { ...c, text: newText } : c)
  }
  
  function handleCaptionClick(caption) {
    if (videoElement) {
      videoElement.currentTime = caption.start
      // Active state is now handled by handleTimeUpdate
    }
  }
</script>

<svelte:window on:mousemove={handleMouseMove} on:mouseup={handleMouseUp} />

<div class="flex-1 flex flex-col ml-16 h-screen">
  <!-- Header -->
  <header class="bg-white border-b border-gray-200 px-6 py-3 flex items-center justify-between">
    <h1 class="text-lg font-semibold text-gray-900">Video Editor ({jobId})</h1>
    <div class="flex items-center space-x-3">
      <button class="px-4 py-2 bg-gray-900 text-white rounded-lg text-sm font-medium hover:bg-gray-800 transition-colors">
        Export Video
      </button>
    </div>
  </header>

  <!-- Main content area -->
  {#if loading}
    <div class="flex-1 flex items-center justify-center text-gray-500 text-xl">Loading editor...</div>
  {:else if error}
    <div class="flex-1 flex items-center justify-center text-red-500 text-xl">Error: {error}</div>
  {:else}
    <div class="flex-1 flex overflow-hidden">
      <!-- Video Player Section -->
      <div class="flex-1 flex flex-col items-center justify-center bg-gray-900 p-8">
        <!-- 9:16 Video Container -->
        <div class="relative h-full max-h-[calc(100vh-280px)] aspect-[9/16] bg-black rounded-lg overflow-hidden shadow-2xl">
          <video
            bind:this={videoElement}
            class="w-full h-full object-contain"
            src={videoUrl}
            on:timeupdate={handleTimeUpdate}
            on:click={togglePlay}
            controls
          >
            <track kind="captions" />
          </video>
          
          <!-- Draggable/Resizable Subtitle Overlay -->
          <div
            class="absolute transform -translate-x-1/2 cursor-move no-select group"
            style="left: {subtitlePosition.x}%; bottom: {subtitlePosition.y}%;"
            on:mousedown={handleSubtitleMouseDown}
          >
            <div 
              class="bg-black/80 text-white px-4 py-2 rounded-lg text-center min-w-[200px] border-2 border-transparent group-hover:border-green-400 transition-all"
              style="width: {subtitleSize.width}px;"
            >
              <p class="text-lg font-medium">{subtitleText}</p>
              
              <!-- Resize handles -->
              <div class="resize-handle absolute -bottom-1 -right-1 w-3 h-3 bg-green-400 rounded-full cursor-se-resize opacity-0 group-hover:opacity-100 transition-opacity"></div>
              <div class="resize-handle absolute -bottom-1 -left-1 w-3 h-3 bg-green-400 rounded-full cursor-sw-resize opacity-0 group-hover:opacity-100 transition-opacity"></div>
            </div>
            
            <!-- Position indicator -->
            <div class="absolute -top-6 left-1/2 transform -translate-x-1/2 bg-gray-800 text-white text-xs px-2 py-1 rounded opacity-0 group-hover:opacity-100 transition-opacity">
              {Math.round(subtitlePosition.x)}%, {Math.round(subtitlePosition.y)}%
            </div>
          </div>
          
          <!-- Guidelines -->
          <div class="absolute inset-0 pointer-events-none opacity-0 hover:opacity-100 transition-opacity">
            <div class="absolute left-1/2 top-0 bottom-0 w-px bg-green-400/50"></div>
            <div class="absolute top-1/2 left-0 right-0 h-px bg-green-400/50"></div>
          </div>
        </div>
        
        <!-- Video Controls -->
        <div class="mt-4 flex items-center space-x-4 bg-gray-800 rounded-full px-6 py-3">
          <button 
            class="text-gray-400 hover:text-white transition-colors"
            on:click={() => videoElement && (videoElement.currentTime -= 5)}
          >
            <SkipBack class="w-5 h-5" />
          </button>
          
          <button 
            class="w-10 h-10 bg-white rounded-full flex items-center justify-center text-gray-900 hover:bg-gray-200 transition-colors"
            on:click={togglePlay}
          >
            {#if isPlaying}
              <Pause class="w-5 h-5" />
            {:else}
              <Play class="w-5 h-5 ml-0.5" />
            {/if}
          </button>
          
          <button 
            class="text-gray-400 hover:text-white transition-colors"
            on:click={() => videoElement && (videoElement.currentTime += 5)}
          >
            <SkipForward class="w-5 h-5" />
          </button>
          
          <div class="w-px h-6 bg-gray-600 mx-2"></div>
          
          <span class="text-gray-300 text-sm font-mono min-w-[100px]">
            {formatTime(currentTime)} / {formatTime(duration)}
          </span>
          
          <div class="flex items-center space-x-2">
            <Volume2 class="w-4 h-4 text-gray-400" />
            <input 
              type="range" 
              min="0" 
              max="1" 
              step="0.1"
              bind:value={volume}
              on:input={() => videoElement && (videoElement.volume = volume)}
              class="w-20 accent-green-500"
            />
          </div>
          
          <button class="text-gray-400 hover:text-white transition-colors ml-2">
            <Maximize class="w-5 h-5" />
          </button>
        </div>
      </div>

      <!-- Timeline Panel -->
      <Timeline 
        {captions} 
        {currentTime}
        onUpdate={handleCaptionUpdate}
        onCaptionClick={handleCaptionClick}
      />
    </div>
  {/if}
</div>
