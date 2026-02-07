<script>
  import VideoEditor from './components/VideoEditor.svelte'
  import Sidebar from './components/Sidebar.svelte'
  import { onMount } from 'svelte'

  let jobId = null

  onMount(() => {
    const pathSegments = window.location.pathname.split('/')
    const editIndex = pathSegments.indexOf('edit')
    if (editIndex > -1 && pathSegments.length > editIndex + 1) {
      jobId = pathSegments[editIndex + 1]
    }
  })
</script>

<main class="flex h-screen w-full overflow-hidden bg-gray-100">
  <Sidebar />
  {#if jobId}
    <VideoEditor {jobId} />
  {:else}
    <div class="flex-1 flex items-center justify-center text-gray-500 text-xl ml-16">
      No video job selected. Please go to the dashboard to select a video to edit.
    </div>
  {/if}
</main>
